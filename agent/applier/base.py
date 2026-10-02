"""Shared dry-run form behavior for Lever, Ashby, and SmartRecruiters."""

import logging
import re
from pathlib import Path

from anthropic import Anthropic
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.answers import answer_custom_question
from agent.applier.confirmation import (
    UNKNOWN_OUTCOME_ERROR,
    confirmation_visible,
    wait_for_confirmation,
)
from agent.applier.greenhouse import (
    ApplierResult,
    _answer_for_choice_question,
    _as_text,
    _fill_label,
    _normalize_label,
    _select_combobox_option,
    extract_resume_text,
    load_profile,
    read_page_description,
)
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
PLATFORM_HOSTS = {
    "lever": ("jobs.lever.co", "jobs.eu.lever.co"),
    "ashby": ("jobs.ashbyhq.com",),
    "smartrecruiters": ("jobs.smartrecruiters.com",),
}
DESCRIPTION_SELECTORS = {
    "lever": ('[data-qa="job-description"]', ".posting-page .content"),
    "ashby": ('[class*="descriptionText"]', "#overview"),
    "smartrecruiters": ('[itemprop="description"]', ".job-sections"),
}


def run_tier1_dry_run(
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
    """Fill supported controls for a Tier 1 ATS; submission is default-off."""
    supported_hosts = PLATFORM_HOSTS.get(job.platform)
    if not supported_hosts or not any(
        re.match(rf"^https://{re.escape(host)}/", job.url, flags=re.IGNORECASE)
        for host in supported_hosts
    ):
        return ApplierResult("failed", {}, None, None, "Job URL does not match its ATS platform.")

    try:
        profile = load_profile(profile_path)
        resume = resume_text if resume_text is not None else extract_resume_text(resume_path)
        if not resume.strip():
            raise ValueError("Resume text is empty")
    except (OSError, ValueError) as error:
        return ApplierResult("failed", {}, None, None, str(error))

    answers: dict[str, str | None] = {}
    suggested: dict[str, str] = {}
    notes: dict[str, str] = {}
    job_description = job.description
    manual_review = False
    resume_uploaded = False
    screenshot_saved = False
    submit_attempted = False
    confirmed = False
    try:
        page.goto(job.url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            LOGGER.info(
                "%s page stayed active; continuing after bounded readiness wait", job.platform
            )
        job_description = job.description or read_page_description(
            page, DESCRIPTION_SELECTORS[job.platform]
        )

        personal = profile.get("personal", {})
        if not isinstance(personal, dict):
            raise ValueError("Profile personal section must be a mapping")
        name_parts = str(personal.get("name", "")).strip().split(maxsplit=1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        first_filled = _fill_label(page, "First Name", first_name, answers)
        last_filled = _fill_label(page, "Last Name", last_name, answers)
        if not first_filled and not last_filled:
            if not _fill_label(page, "Name", _as_text(personal.get("name")), answers):
                notes["Name"] = "No name field was found on this form."
        elif first_filled != last_filled:
            manual_review = True
            notes["Name"] = "Only one of First Name / Last Name was found."

        github = _as_text(personal.get("github"))
        website = _as_text(personal.get("website"))
        if website and github and website.rstrip("/") == github.rstrip("/"):
            website = None
            answers["Website"] = None
            notes["Website"] = "Profile website duplicates the GitHub URL; left blank."
            manual_review = True
        for label, value in (
            ("Email", _as_text(personal.get("email"))),
            ("Phone", _as_text(personal.get("phone"))),
            ("LinkedIn", _as_text(personal.get("linkedin"))),
            ("Website", website),
            ("GitHub URL", github),
        ):
            if value and not _fill_label(page, label, value, answers):
                notes[label] = "Skipped: no matching field on this form."

        file_inputs = page.locator('input[type="file"]')
        if file_inputs.count() == 1:
            file_inputs.set_input_files(str(resume_path))
            try:
                page.get_by_text(resume_path.name, exact=True).wait_for(
                    state="visible", timeout=45000
                )
                resume_uploaded = True
            except PlaywrightTimeoutError:
                resume_uploaded = bool(
                    file_inputs.count()
                    and file_inputs.evaluate(
                        "input => Boolean(input.files && input.files.length > 0)"
                    )
                )
        if not resume_uploaded:
            manual_review = True
            notes["Resume"] = (
                "Resume upload failed or could not be verified."
                if file_inputs.count() == 1
                else f"Expected one file input, found {file_inputs.count()}."
            )

        form = _find_form(page)
        labels = form.locator("label")
        for index in range(labels.count()):
            label_text = " ".join(labels.nth(index).inner_text().split())
            normalized = _normalize_label(label_text)
            if not label_text or normalized in STANDARD_FIELDS or "resume" in normalized:
                if normalized == "cover letter":
                    notes[label_text] = "Skipped: cover letters are not automated."
                continue
            accessible_label = re.sub(r"\s*\*\s*$", "", label_text).strip()
            locator = page.get_by_label(accessible_label, exact=False)
            if locator.count() != 1:
                manual_review = True
                answers[label_text] = None
                notes[label_text] = f"Expected one matching control, found {locator.count()}."
                continue
            if (locator.get_attribute("role") or "").casefold() == "combobox":
                choice = _answer_for_choice_question(label_text, profile)
                if choice is None or not _select_combobox_option(page, locator, choice):
                    manual_review = True
                    answers[label_text] = None
                    notes[label_text] = (
                        "No configured answer for this choice question."
                        if choice is None
                        else f"Configured answer {choice!r} could not be selected."
                    )
                else:
                    answers[label_text] = choice
                continue

            tag = locator.evaluate("element => element.tagName.toLowerCase()")
            field_type = (locator.get_attribute("type") or "text").lower()
            if tag not in {"input", "textarea"} or field_type not in {
                "text", "email", "tel", "url", ""
            }:
                manual_review = True
                answers[label_text] = None
                notes[label_text] = f"Unsupported control type ({tag}/{field_type})."
                continue
            try:
                decision = answer_custom_question(
                    label_text,
                    profile,
                    resume,
                    client=answers_client,
                    job_context={
                        "company": job.company,
                        "title": job.title,
                        "description": job_description,
                    },
                )
            except Exception as error:
                LOGGER.exception("Could not answer %s question %r", job.platform, label_text)
                notes[label_text] = f"Answer service error: {type(error).__name__}."
                decision = None
            if decision is None or decision.answer is None:
                manual_review = True
                answers[label_text] = None
                if decision is not None:
                    notes[label_text] = decision.reason or "No grounded answer was found."
                continue
            if decision.needs_manual_review:
                notes[label_text] = decision.reason or "Answer needs candidate review."
                if decision.is_motivation_draft and fill_reviewed_motivation_drafts:
                    locator.fill(decision.answer)
                    answers[label_text] = decision.answer
                else:
                    manual_review = True
                    answers[label_text] = None
                    suggested[label_text] = decision.answer
                continue
            locator.fill(decision.answer)
            answers[label_text] = decision.answer

        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=True)
        screenshot_saved = True
        if not form.evaluate("element => element.checkValidity()"):
            manual_review = True
        if submit_live and not manual_review:
            button = form.get_by_role("button", name=re.compile(r"submit application|submit", re.I))
            if button.count() != 1:
                raise RuntimeError("Could not uniquely identify the application submit button")
            # Text already on the page cannot prove that this submission succeeded.
            confirmation_preexisting = confirmation_visible(page)
            submit_attempted = True
            button.click()
            confirmed = not confirmation_preexisting and wait_for_confirmation(page)
    except Exception as error:
        if submit_attempted:
            LOGGER.exception(
                "%s submit outcome unknown for %s; verify manually", job.platform, job.url
            )
            return ApplierResult(
                "unknown", answers, str(screenshot_path), None,
                f"{UNKNOWN_OUTCOME_ERROR} {type(error).__name__}: {error}",
                resume_uploaded, suggested, field_notes=notes, job_description=job_description,
            )
        LOGGER.exception("%s applier failed for %s", job.platform, job.url)
        return ApplierResult(
            "failed", answers, str(screenshot_path) if screenshot_saved else None, None,
            f"{type(error).__name__}: {error}", resume_uploaded, suggested,
            field_notes=notes, job_description=job_description,
        )

    if submit_attempted and not confirmed:
        LOGGER.warning(
            "%s submit outcome unknown for %s; verify manually", job.platform, job.url
        )
        return ApplierResult(
            "unknown", answers, str(screenshot_path), None, UNKNOWN_OUTCOME_ERROR,
            resume_uploaded, suggested, field_notes=notes, job_description=job_description,
        )
    if submit_live and manual_review:
        LOGGER.warning("Not submitting %s because it still needs manual review", job.url)
    elif fill_reviewed_motivation_drafts and suggested:
        manual_review = True
    status = "manual_review" if manual_review else "applied" if submit_live else "dry_run_ready"
    return ApplierResult(
        status,
        answers,
        str(screenshot_path),
        None,
        resume_uploaded=resume_uploaded,
        suggested_answers=suggested,
        submitted=submit_live and not manual_review,
        field_notes=notes,
        job_description=job_description,
    )


def _find_form(page: Page) -> Locator:
    for selector in ("form", "[role='form']", "main"):
        locator = page.locator(selector)
        if locator.count():
            return locator.first
    raise RuntimeError("Could not locate the application form")
