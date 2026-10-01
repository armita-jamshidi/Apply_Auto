"""Dry-run-only form filling for Greenhouse application pages."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from anthropic import Anthropic
from playwright.sync_api import Page
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

    status: Literal["dry_run_ready", "manual_review", "failed"]
    answers: dict[str, str | None]
    screenshot_path: str | None
    tailored_resume_path: str | None
    error: str | None = None


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
) -> ApplierResult:
    """Fill supported Greenhouse fields, save a full-page screenshot, and never submit."""
    answers: dict[str, str | None] = {}
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
    try:
        page.goto(job.url, wait_until="domcontentloaded")
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

        for label, key in (
            ("Email", "email"),
            ("Phone", "phone"),
            ("LinkedIn", "linkedin"),
            ("Website", "website"),
            ("GitHub URL", "github"),
        ):
            _fill_label(page, label, _as_text(personal.get(key)), answers)

        if not _upload_resume(page, resume_path):
            manual_review = True
            LOGGER.info("Resume upload control not found for %s", job.url)

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
            locator = page.get_by_label(label_text, exact=True)
            if locator.count() != 1:
                manual_review = True
                answers[label_text] = None
                continue

            combobox = page.get_by_role("combobox", name=label_text, exact=False)
            if combobox.count():
                manual_review = True
                answers[label_text] = None
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
                )
            except Exception:
                LOGGER.exception("Could not answer custom Greenhouse question %r", label_text)
                answers[label_text] = None
                manual_review = True
                continue
            answers[label_text] = decision.answer
            if decision.needs_manual_review or decision.answer is None:
                manual_review = True
                continue
            locator.fill(decision.answer)

        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=True)
        screenshot_saved = True
    except Exception as error:
        LOGGER.exception("Greenhouse dry run failed for %s", job.url)
        return ApplierResult(
            status="failed",
            answers=answers,
            screenshot_path=str(screenshot_path) if screenshot_saved else None,
            tailored_resume_path=None,
            error=f"{type(error).__name__}: {error}",
        )

    return ApplierResult(
        status="manual_review" if manual_review else "dry_run_ready",
        answers=answers,
        screenshot_path=str(screenshot_path),
        tailored_resume_path=None,
    )


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
        return True

    labels = page.locator("#application-form label")
    for index in range(labels.count()):
        label_text = " ".join(labels.nth(index).inner_text().split())
        if re.search(r"\b(?:resume|cv)\b", label_text, flags=re.IGNORECASE):
            locator = page.get_by_label(label_text, exact=True)
            if locator.count() == 1 and (locator.get_attribute("type") or "").lower() == "file":
                locator.set_input_files(str(resume_path))
                return True
    return False


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip().rstrip("*").strip().casefold()


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None
