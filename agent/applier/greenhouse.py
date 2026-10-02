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
from agent.profile_schema import CandidateProfile
from agent.types import JobListing

LOGGER = logging.getLogger(__name__)
STANDARD_FIELDS = {
    "first name",
    "last name",
    "name",
    "email",
    "phone",
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
    """Fill supported Greenhouse fields, save a full-page screenshot, and never submit."""
    answers: dict[str, str | None] = {}
    suggested_answers: dict[str, str] = {}
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
    submitted = False
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
            _fill_label(page, "Name", name, answers)
        elif first_filled != last_filled:
            manual_review = True

        github_url = _as_text(personal.get("github"))
        website_url = _as_text(personal.get("website"))
        if website_url and github_url and website_url.rstrip("/") == github_url.rstrip("/"):
            website_url = None
            answers["Website"] = None
            manual_review = True

        for label, value in (
            ("Email", _as_text(personal.get("email"))),
            ("Phone", _as_text(personal.get("phone"))),
            ("LinkedIn", _as_text(personal.get("linkedin"))),
            ("Website", website_url),
            ("GitHub URL", github_url),
        ):
            _fill_label(page, label, value, answers)

        resume_uploaded = _upload_resume(page, resume_path)
        if not resume_uploaded:
            manual_review = True
            LOGGER.info("Resume upload failed or could not be verified for %s", job.url)

        labels = page.locator("#application-form label")
        for index in range(labels.count()):
            label_text = " ".join(labels.nth(index).inner_text().split())
            normalized = _normalize_label(label_text)
            if (
                not label_text
                or normalized in STANDARD_FIELDS
                or "resume" in normalized
                or normalized == "cv"
            ):
                continue
            accessible_label = re.sub(r"\s*\*\s*$", "", label_text).strip()
            locator = page.get_by_label(accessible_label, exact=False)
            if locator.count() != 1:
                manual_review = True
                answers[label_text] = None
                continue

            control_role = (locator.get_attribute("role") or "").casefold()
            if control_role == "combobox":
                decision = _answer_for_choice_question(label_text, profile)
                if decision is None:
                    manual_review = True
                    answers[label_text] = None
                    continue
                if not _select_combobox_option(page, locator, decision):
                    manual_review = True
                    answers[label_text] = None
                    continue
                answers[label_text] = decision
                continue

            control_tag = locator.evaluate("element => element.tagName.toLowerCase()")
            control_type = (locator.get_attribute("type") or "text").lower()
            if control_tag not in {"input", "textarea"} or control_type not in {
                "text",
                "email",
                "tel",
                "url",
                "",
            }:
                manual_review = True
                answers[label_text] = None
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
            except Exception:
                LOGGER.exception("Could not answer custom Greenhouse question %r", label_text)
                answers[label_text] = None
                manual_review = True
                continue
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
            submit_button.click()
            submitted = True
            try:
                page.get_by_text(re.compile(r"application (?:submitted|received)", re.I)).wait_for(
                    state="visible",
                    timeout=30000,
                )
            except PlaywrightTimeoutError:
                LOGGER.warning(
                    "Submit click completed but confirmation was not detected for %s", job.url
                )
    except Exception as error:
        LOGGER.exception("Greenhouse dry run failed for %s", job.url)
        return ApplierResult(
            status="applied" if submitted else "failed",
            answers=answers,
            screenshot_path=str(screenshot_path) if screenshot_saved else None,
            tailored_resume_path=None,
            error=f"{type(error).__name__}: {error}",
            resume_uploaded=resume_uploaded,
            suggested_answers=suggested_answers,
            submitted=submitted,
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


def _select_combobox_option(page: Page, combobox: Locator, answer: str) -> bool:
    """Select and verify a visible ARIA combobox option by exact accessible name."""
    try:
        combobox.click()
        option = page.get_by_role("option", name=answer, exact=True)
        if option.count() != 1:
            return False
        option.click()
        return (combobox.input_value() or "").strip().casefold() == answer.casefold()
    except PlaywrightError:
        return False


def _answer_for_choice_question(question: str, profile: dict[str, object]) -> str | None:
    """Return an explicitly configured Yes/No answer for an accessible choice control."""
    normalized = _normalize_label(question)
    if "ai policy for application" in normalized or (
        re.search(r"\b(?:ai|artificial intelligence)\b", normalized)
        and re.search(r"\b(?:policy|policies|guidelines?)\b", normalized)
    ):
        return "Yes"

    has_prior_reference = bool(
        re.search(r"\b(?:ever|before|previously|prior|in the past)\b", normalized)
    )
    if has_prior_reference and re.search(r"\binterview(?:ed|ing)?\b", normalized):
        return "No"
    if has_prior_reference and re.search(r"\b(?:applied|application)\b", normalized):
        return "No"
    if re.search(r"\b(?:meet|satisfy)\b", normalized) and re.search(
        r"\b(?:qualifications?|requirements?|criteria)\b", normalized
    ):
        personal = profile.get("personal", {})
        if isinstance(personal, dict) and personal.get("meets_job_requirements") is True:
            return "Yes"
    return None


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


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip().rstrip("*").strip().casefold()


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None
