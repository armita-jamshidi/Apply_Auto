"""Dry-run-only form filling for Greenhouse application pages."""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from anthropic import Anthropic
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pypdf import PdfReader

from agent.answers import answer_custom_question
from agent.applier.choices import (
    DATE_LABELS,
    ChoiceGroups,
    choose_combobox_option,
    choose_select_option,
    todays_date,
)
from agent.applier.confirmation import (
    UNKNOWN_OUTCOME_ERROR,
    confirmation_visible,
    wait_for_confirmation,
)
from agent.profile_schema import CandidateProfile
from agent.types import JobListing

LOGGER = logging.getLogger(__name__)
REQUIRED_MARKER = re.compile(r"\s*[*\u2731]+\s*$")
CHOICE_KINDS = frozenset({"checkbox", "radio", "choice-group"})
NO_CONTROL_NOTE = "No form control is linked to this label (custom widget); answer manually."
OTHER_UPLOAD_NOTE = "Skipped: file uploads other than the resume are not automated."
# Reads a label's visible text (no nested options or hidden error text) and what it labels.
# A label with no control that heads checkbox/radio options reports kind "choice-group".
LABEL_INFO_SCRIPT = """label => {
  const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
  const visibleText = element => {
    const parts = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const parent = walker.currentNode.parentElement;
      if (!parent || parent.closest('select, option, button, textarea')) continue;
      if (parent.checkVisibility && !parent.checkVisibility()) continue;
      parts.push(walker.currentNode.textContent);
    }
    return clean(parts.join(' '));
  };
  const choices = 'input[type="checkbox"], input[type="radio"]';
  const boxOf = node => node.closest(
    'fieldset, [role="group"], [role="radiogroup"], .application-question'
  );
  const text = visibleText(label);
  const control = label.control;
  if (!control) {
    const box = boxOf(label);
    const headsChoices = box && box.querySelector(choices);
    return {text, kind: headsChoices ? 'choice-group' : null, group: headsChoices ? text : null};
  }
  const kind = control.type || control.tagName.toLowerCase();
  let group = null;
  if (kind === 'checkbox' || kind === 'radio') {
    const box = boxOf(control);
    const heading = box && (
      box.querySelector('legend, .application-label, .text')
      || [...box.querySelectorAll('label')].find(candidate => !candidate.control)
    );
    group = (heading && visibleText(heading)) || control.name || null;
  }
  return {text, kind, group};
}"""
STANDARD_FIELDS = {
    "first name",
    "last name",
    "name",
    "full name",
    "email",
    "email address",
    "phone",
    "phone number",
    "linkedin",
    "linkedin profile",
    "linkedin profile url",
    "website",
    "website, blog, or portfolio",
    "github",
    "github url",
    "resume",
    "cv",
    "cover letter",
}

@dataclass(frozen=True, slots=True)
class LabelInfo:
    """A form label's own text and the control it labels; kind None means no control."""

    text: str
    kind: str | None
    group: str | None = None


@dataclass(frozen=True, slots=True)
class ApplierResult:
    """Outcome of filling a Greenhouse form without submitting it."""

    status: Literal["dry_run_ready", "manual_review", "failed", "applied", "unknown"]
    answers: dict[str, str | None]
    screenshot_path: str | None
    tailored_resume_path: str | None
    error: str | None = None
    resume_uploaded: bool = False
    suggested_answers: dict[str, str] = field(default_factory=dict)
    submitted: bool = False
    field_notes: dict[str, str] = field(default_factory=dict)
    job_description: str = ""


def load_profile(profile_path: Path) -> dict[str, object]:
    """Load and validate a private YAML profile, including GitHub and projects."""
    with profile_path.open(encoding="utf-8") as stream:
        profile_data = yaml.safe_load(stream)
    return CandidateProfile.model_validate(profile_data).as_profile_dict()


def extract_resume_text(resume_path: Path) -> str:
    """Extract selectable text from a PDF resume; image-only PDFs need manual review."""
    if not resume_path.is_file():
        raise FileNotFoundError(f"Resume PDF not found: {resume_path}")
    pages = PdfReader(str(resume_path)).pages
    text = "\n".join(page.extract_text() or "" for page in pages).strip()
    if not text:
        raise ValueError("Resume has no extractable text; OCR/manual review is required")
    return text


def run_greenhouse_dry_run(
    page: Page,
    job: JobListing,
    profile_path: Path,
    resume_path: Path,
    screenshot_path: Path,
    *,
    answers_client: Anthropic | None = None,
    resume_text: str | None = None,
    fill_reviewed_motivation_drafts: bool = False,
    submit_live: bool = False,
) -> ApplierResult:
    """Fill Greenhouse fields and screenshot; submit only when live and the form is complete."""
    answers: dict[str, str | None] = {}
    suggested_answers: dict[str, str] = {}
    notes: dict[str, str] = {}
    job_description = job.description
    try:
        profile = load_profile(profile_path)
        extracted_resume = (
            resume_text if resume_text is not None else extract_resume_text(resume_path)
        )
        if not extracted_resume.strip():
            raise ValueError("Resume text is empty; custom questions require manual review")
    except (OSError, ValueError) as error:
        return ApplierResult("failed", answers, None, None, str(error))

    manual_review = False
    screenshot_saved = False
    resume_uploaded = False
    submit_attempted = False
    confirmed = False
    try:
        page.goto(job.url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            LOGGER.info("Greenhouse page stayed active; continuing after bounded readiness wait")
        job_description = job.description or read_page_description(
            page, (".job__description", "#content")
        )
        personal = profile["personal"]
        assert isinstance(personal, dict)
        name = str(personal.get("name", "")).strip()
        name_parts = name.split(maxsplit=1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        first_filled = _fill_label(page, "First Name", first_name, answers)
        last_filled = _fill_label(page, "Last Name", last_name, answers)
        if not first_filled and not last_filled:
            if not _fill_label(page, "Name", name, answers):
                notes["Name"] = "No name field was found on this form."
        elif first_filled != last_filled:
            manual_review = True
            notes["Name"] = "Only one of First Name / Last Name was found."

        github_url = _as_text(personal.get("github"))
        website_url = _as_text(personal.get("website"))
        if website_url and github_url and website_url.rstrip("/") == github_url.rstrip("/"):
            website_url = None
            answers["Website"] = None
            notes["Website"] = "Profile website duplicates the GitHub URL; left blank."
            manual_review = True

        for label, value in (
            ("Email", _as_text(personal.get("email"))),
            ("Phone", _as_text(personal.get("phone"))),
            ("LinkedIn", _as_text(personal.get("linkedin"))),
            ("Website", website_url),
            ("GitHub URL", github_url),
        ):
            if value and not _fill_label(page, label, value, answers):
                notes[label] = "Skipped: no matching field on this form."

        resume_uploaded = _upload_resume(page, resume_path)
        if not resume_uploaded:
            manual_review = True
            notes["Resume"] = "Resume upload failed or could not be verified."
            LOGGER.info("Resume upload failed or could not be verified for %s", job.url)

        labels = page.locator("#application-form label")
        choice_groups = ChoiceGroups(profile)
        for index in range(labels.count()):
            info = inspect_label(labels.nth(index))
            label_text = info.text
            normalized = _normalize_label(label_text)
            if (
                not label_text
                or normalized in STANDARD_FIELDS
                or "resume" in normalized
                or normalized == "cv"
            ):
                if normalized == "cover letter":
                    notes[label_text] = "Skipped: cover letters are not automated."
                continue
            if info.kind == "file":
                notes[label_text] = OTHER_UPLOAD_NOTE
                continue
            if info.kind in CHOICE_KINDS:
                question = info.group or label_text
                if info.kind == "choice-group":
                    choice_groups.add(question)
                else:
                    choice_groups.add(question, label_text, labels.nth(index))
                continue
            locator = find_labelled_control(page, label_text)
            if locator.count() != 1:
                manual_review = True
                answers[label_text] = None
                notes[label_text] = (
                    NO_CONTROL_NOTE
                    if info.kind is None
                    else f"Expected one matching control, found {locator.count()}."
                )
                continue

            control_role = (locator.get_attribute("role") or "").casefold()
            if control_role == "combobox":
                choice, problem = choose_combobox_option(page, locator, label_text, profile)
                answers[label_text] = choice
                if problem:
                    manual_review = True
                    notes[label_text] = problem
                continue

            control_tag = locator.evaluate("element => element.tagName.toLowerCase()")
            control_type = (locator.get_attribute("type") or "text").lower()
            if control_tag == "select":
                choice, problem = choose_select_option(locator, label_text, profile)
                answers[label_text] = choice
                if problem:
                    manual_review = True
                    notes[label_text] = problem
                continue
            if control_tag not in {"input", "textarea"} or control_type not in {
                "text",
                "email",
                "tel",
                "url",
                "",
            }:
                manual_review = True
                answers[label_text] = None
                notes[label_text] = f"Unsupported control type ({control_tag}/{control_type})."
                continue
            if normalized in DATE_LABELS:
                answers[label_text] = todays_date()
                locator.fill(answers[label_text])
                continue

            try:
                decision = answer_custom_question(
                    label_text,
                    profile,
                    extracted_resume,
                    client=answers_client,
                    job_context={
                        "company": job.company,
                        "title": job.title,
                        "description": job_description,
                    },
                )
            except Exception as error:
                LOGGER.exception("Could not answer custom Greenhouse question %r", label_text)
                answers[label_text] = None
                notes[label_text] = f"Answer service error: {type(error).__name__}."
                manual_review = True
                continue
            if decision.reason and (decision.needs_manual_review or decision.answer is None):
                notes[label_text] = decision.reason
            if decision.needs_manual_review or decision.answer is None:
                if decision.answer is not None and decision.is_motivation_draft:
                    if fill_reviewed_motivation_drafts:
                        locator.fill(decision.answer)
                        answers[label_text] = decision.answer
                    else:
                        manual_review = True
                        answers[label_text] = None
                        suggested_answers[label_text] = decision.answer
                else:
                    manual_review = True
                    answers[label_text] = None
                if decision.answer is not None and not decision.is_motivation_draft:
                    suggested_answers[label_text] = decision.answer
                continue
            locator.fill(decision.answer)
            answers[label_text] = decision.answer

        if choice_groups.apply(answers, notes):
            manual_review = True

        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=True)
        screenshot_saved = True
        form_is_valid = page.locator("#application-form").evaluate("form => form.checkValidity()")
        if not form_is_valid:
            manual_review = True
        if submit_live and not manual_review:
            submit_button = page.get_by_role("button", name=re.compile(r"submit application", re.I))
            if submit_button.count() != 1:
                raise RuntimeError("Could not uniquely identify the Greenhouse submit button")
            # Text already on the page cannot prove that this submission succeeded.
            confirmation_preexisting = confirmation_visible(page)
            submit_attempted = True
            submit_button.click()
            confirmed = not confirmation_preexisting and wait_for_confirmation(page)
    except Exception as error:
        if submit_attempted:
            LOGGER.exception("Greenhouse submit outcome unknown for %s; verify manually", job.url)
            return ApplierResult(
                status="unknown",
                answers=answers,
                screenshot_path=str(screenshot_path),
                tailored_resume_path=None,
                error=f"{UNKNOWN_OUTCOME_ERROR} {type(error).__name__}: {error}",
                resume_uploaded=resume_uploaded,
                suggested_answers=suggested_answers,
                field_notes=notes,
                job_description=job_description,
            )
        LOGGER.exception("Greenhouse dry run failed for %s", job.url)
        return ApplierResult(
            status="failed",
            answers=answers,
            screenshot_path=str(screenshot_path) if screenshot_saved else None,
            tailored_resume_path=None,
            error=f"{type(error).__name__}: {error}",
            resume_uploaded=resume_uploaded,
            suggested_answers=suggested_answers,
            field_notes=notes,
            job_description=job_description,
        )

    if submit_attempted and not confirmed:
        LOGGER.warning("Greenhouse submit outcome unknown for %s; verify manually", job.url)
        return ApplierResult(
            status="unknown",
            answers=answers,
            screenshot_path=str(screenshot_path),
            tailored_resume_path=None,
            error=UNKNOWN_OUTCOME_ERROR,
            resume_uploaded=resume_uploaded,
            suggested_answers=suggested_answers,
            field_notes=notes,
            job_description=job_description,
        )

    return ApplierResult(
        status=(
            "manual_review"
            if manual_review
            else "applied"
            if submit_live
            else "dry_run_ready"
        ),
        answers=answers,
        screenshot_path=str(screenshot_path),
        tailored_resume_path=None,
        resume_uploaded=resume_uploaded,
        suggested_answers=suggested_answers,
        submitted=submit_live and not manual_review,
        field_notes=notes,
        job_description=job_description,
    )


def read_page_description(page: Page, selectors: tuple[str, ...]) -> str:
    """Return the first non-empty job description text found by the given selectors."""
    for selector in selectors:
        try:
            description_locator = page.locator(selector)
            if description_locator.count():
                description = description_locator.first.inner_text().strip()
                if description:
                    return description
        except Exception:
            LOGGER.debug("Could not read job description from selector %s", selector)
    return ""


def _fill_label(page: Page, label: str, value: str | None, answers: dict[str, str | None]) -> bool:
    if not value:
        return False
    locator = page.get_by_label(label, exact=False)
    if locator.count() == 0:
        return False
    locator.first.fill(value)
    answers[label] = value
    return True


def _upload_resume(page: Page, resume_path: Path) -> bool:
    resume_input = page.locator("#resume")
    if resume_input.count() == 1 and (resume_input.get_attribute("type") or "").lower() == "file":
        resume_input.set_input_files(str(resume_path))
        return _wait_for_resume_attachment(page, resume_input, resume_path)

    labels = page.locator("#application-form label")
    for index in range(labels.count()):
        label_text = " ".join(labels.nth(index).inner_text().split())
        if re.search(r"\b(?:resume|cv)\b", label_text, flags=re.IGNORECASE):
            locator = page.get_by_label(label_text, exact=True)
            if locator.count() == 1 and (locator.get_attribute("type") or "").lower() == "file":
                locator.set_input_files(str(resume_path))
                return _wait_for_resume_attachment(page, locator, resume_path)
    return False


def _wait_for_resume_attachment(page: Page, file_input: Locator, resume_path: Path) -> bool:
    try:
        page.get_by_text(resume_path.name, exact=True).wait_for(state="visible", timeout=45000)
        return True
    except PlaywrightTimeoutError:
        try:
            return bool(
                file_input.count() == 1
                and file_input.evaluate("input => Boolean(input.files && input.files.length > 0)")
            )
        except PlaywrightError:
            return False


def inspect_label(label: Locator) -> LabelInfo:
    """Describe a label; if the browser cannot inspect it, fall back to its visible text."""
    visible_text = " ".join(label.inner_text().split())
    try:
        data = label.evaluate(LABEL_INFO_SCRIPT)
    except PlaywrightError:
        return LabelInfo(visible_text, "unknown")
    if not isinstance(data, dict):
        return LabelInfo(visible_text, "unknown")
    return LabelInfo(
        str(data.get("text") or visible_text),
        data.get("kind"),
        data.get("group"),
    )


def find_labelled_control(page: Page, label_text: str) -> Locator:
    """Find a control by its label without the required marker, preferring an exact match."""
    accessible_label = strip_required_marker(label_text)
    exact = page.get_by_label(accessible_label, exact=True)
    if exact.count() == 1:
        return exact
    return page.get_by_label(accessible_label, exact=False)


def strip_required_marker(label: str) -> str:
    return REQUIRED_MARKER.sub("", " ".join(label.split())).strip()


def _normalize_label(label: str) -> str:
    return strip_required_marker(label).casefold()


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None
