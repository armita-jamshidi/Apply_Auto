"""Shared dry-run form behavior for Lever, Ashby, and SmartRecruiters."""

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from anthropic import Anthropic
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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
from agent.applier.greenhouse import (
    CHOICE_KINDS,
    NO_CONTROL_NOTE,
    OTHER_UPLOAD_NOTE,
    ApplierResult,
    _as_text,
    _fill_label,
    _normalize_label,
    extract_resume_text,
    find_labelled_control,
    inspect_label,
    load_profile,
    read_page_description,
)
from agent.types import JobListing

LOGGER = logging.getLogger(__name__)
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
PLATFORM_HOSTS = {
    "lever": ("jobs.lever.co", "jobs.eu.lever.co"),
    "ashby": ("jobs.ashbyhq.com",),
    "smartrecruiters": ("jobs.smartrecruiters.com",),
}
DESCRIPTION_SELECTORS = {
    "lever": (".posting-page .content", '[data-qa="job-description"]'),
    "ashby": ('[class*="descriptionText"]', "#overview"),
    "smartrecruiters": ('[itemprop="description"]', ".job-sections"),
}
# Discovery stores each posting's overview page; the form lives on a separate page.
APPLICATION_SUFFIXES = {"lever": "/apply", "ashby": "/application"}
SMARTRECRUITERS_FORM_PATH = "/oneclick-ui/"
BOT_CHALLENGE_HOSTS = ("captcha-delivery.com", "challenges.cloudflare.com", "hcaptcha.com")
RESUME_LABEL = re.compile(r"^(?!.*autofill).*\b(?:resume|cv)\b", re.IGNORECASE | re.DOTALL)
FILE_INPUT_LABEL_SCRIPT = (
    "input => Array.from(input.labels || []).map(label => label.innerText).join(' ')"
)
# Works for a <form> or any container, such as Ashby's form-less application page.
FORM_VALIDITY_SCRIPT = (
    "element => element.tagName === 'FORM' ? element.checkValidity() : "
    "Array.from(element.querySelectorAll('input, select, textarea'))"
    ".every(control => control.checkValidity())"
)


def application_urls(platform: str, url: str) -> tuple[str | None, str | None]:
    """Return (overview URL, form URL); None means unknown until the page is opened."""
    parts = urlsplit(url)
    if platform == "smartrecruiters":
        if parts.path.startswith(SMARTRECRUITERS_FORM_PATH):
            return None, url
        return url, None
    suffix = APPLICATION_SUFFIXES.get(platform)
    if suffix is None:
        return url, url
    path = parts.path.rstrip("/")
    if path.endswith(suffix):
        overview = urlunsplit(parts._replace(path=path[: -len(suffix)], query="", fragment=""))
        return overview, url
    return url, urlunsplit(parts._replace(path=path + suffix))


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
    human_challenge_wait_ms: int = 0,
) -> ApplierResult:
    """Fill supported controls for a Tier 1 ATS; submission is default-off.

    With human_challenge_wait_ms, a CAPTCHA is left for the person at the browser to solve;
    filling continues once the form appears. The agent never solves challenges itself.
    """
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
        selectors = DESCRIPTION_SELECTORS[job.platform]
        overview_url, form_url = application_urls(job.platform, job.url)
        if overview_url and (not job_description or form_url is None):
            _open(page, overview_url, job.platform)
            if not job_description:
                job_description = read_page_description(page, selectors)
            if form_url is None:
                form_url = _find_smartrecruiters_form_url(page, supported_hosts)
        if form_url is None:
            raise RuntimeError("Could not find the application form link on the job page")
        _open(page, form_url, job.platform)
        if not job_description:
            job_description = read_page_description(page, selectors)

        challenge = _bot_challenge(page)
        if challenge and human_challenge_wait_ms:
            LOGGER.warning(
                "The application page shows %s. Solve it in the browser window; "
                "filling continues when the form appears (waiting up to %d minutes).",
                challenge,
                human_challenge_wait_ms // 60000,
            )
            try:
                page.locator("input, textarea, select").first.wait_for(
                    state="attached", timeout=human_challenge_wait_ms
                )
                _open_wait(page, job.platform)
            except PlaywrightTimeoutError:
                LOGGER.warning("The challenge was not completed in time for %s", job.url)
            challenge = _bot_challenge(page)
        if challenge:
            LOGGER.warning("%s form for %s is behind %s", job.platform, job.url, challenge)
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
            notes["Application page"] = (
                f"Blocked by {challenge}; complete this application manually in a browser."
            )
            notes["Resume"] = "Not attempted: the application page was blocked."
            return ApplierResult(
                "manual_review", answers, str(screenshot_path), None,
                f"Application page is behind {challenge}.",
                field_notes=notes, job_description=job_description,
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

        resume_input, resume_problem = _resume_input(page)
        if resume_input is not None:
            resume_input.set_input_files(str(resume_path))
            try:
                page.get_by_text(resume_path.name, exact=True).wait_for(
                    state="visible", timeout=45000
                )
                resume_uploaded = True
            except PlaywrightTimeoutError:
                resume_uploaded = bool(
                    resume_input.evaluate(
                        "input => Boolean(input.files && input.files.length > 0)"
                    )
                )
            if not resume_uploaded:
                resume_problem = "Resume upload failed or could not be verified."
        if not resume_uploaded:
            manual_review = True
            notes["Resume"] = resume_problem or "Resume upload could not be verified."

        form = _find_form(page)
        labels = form.locator("label")
        choice_groups = ChoiceGroups(profile)
        for index in range(labels.count()):
            info = inspect_label(labels.nth(index))
            label_text = info.text
            normalized = _normalize_label(label_text)
            if not label_text or normalized in STANDARD_FIELDS or "resume" in normalized:
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
            if (locator.get_attribute("role") or "").casefold() == "combobox":
                choice, problem = choose_combobox_option(page, locator, label_text, profile)
                answers[label_text] = choice
                if problem:
                    manual_review = True
                    notes[label_text] = problem
                continue

            tag = locator.evaluate("element => element.tagName.toLowerCase()")
            field_type = (locator.get_attribute("type") or "text").lower()
            if tag == "select":
                choice, problem = choose_select_option(locator, label_text, profile)
                answers[label_text] = choice
                if problem:
                    manual_review = True
                    notes[label_text] = problem
                continue
            if tag not in {"input", "textarea"} or field_type not in {
                "text", "email", "tel", "url", ""
            }:
                manual_review = True
                answers[label_text] = None
                notes[label_text] = f"Unsupported control type ({tag}/{field_type})."
                continue
            if normalized in DATE_LABELS:
                answers[label_text] = todays_date()
                locator.fill(answers[label_text])
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

        if choice_groups.apply(answers, notes):
            manual_review = True

        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=True)
        screenshot_saved = True
        if not form.evaluate(FORM_VALIDITY_SCRIPT):
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


def _open(page: Page, url: str, platform: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    _open_wait(page, platform)


def _open_wait(page: Page, platform: str) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        LOGGER.info("%s page stayed active; continuing after bounded readiness wait", platform)


def _find_smartrecruiters_form_url(page: Page, hosts: tuple[str, ...]) -> str | None:
    """Return the first same-host one-click application link on a SmartRecruiters posting."""
    links = page.locator(f'a[href*="{SMARTRECRUITERS_FORM_PATH}"]')
    for index in range(links.count()):
        href = links.nth(index).get_attribute("href") or ""
        parts = urlsplit(href)
        if parts.scheme == "https" and (parts.hostname or "").casefold() in hosts:
            return href
    return None


def _bot_challenge(page: Page) -> str | None:
    """Name a blocking bot challenge when the page shows one instead of a form."""
    if page.locator("input, textarea, select").count():
        return None
    frames = page.locator("iframe")
    for index in range(frames.count()):
        src = (frames.nth(index).get_attribute("src") or "").casefold()
        if any(host in src for host in BOT_CHALLENGE_HOSTS):
            return "a bot-protection challenge (CAPTCHA)"
    return None


def _resume_input(page: Page) -> tuple[Locator | None, str | None]:
    """Pick the resume file input, ignoring extras such as Ashby's autofill-from-resume upload."""
    file_inputs = page.locator('input[type="file"]')
    count = file_inputs.count()
    if count == 1:
        return file_inputs.nth(0), None
    if count == 0:
        return None, "No file upload control was found."
    labelled = [
        file_inputs.nth(index)
        for index in range(count)
        if RESUME_LABEL.search(file_inputs.nth(index).evaluate(FILE_INPUT_LABEL_SCRIPT) or "")
    ]
    if len(labelled) == 1:
        return labelled[0], None
    return None, f"Found {count} file inputs and could not tell which one takes the resume."


def _find_form(page: Page) -> Locator:
    # Ashby renders its application without a form or main element.
    for selector in ("form", "[role='form']", "main", "body"):
        locator = page.locator(selector)
        if locator.count():
            return locator.first
    raise RuntimeError("Could not locate the application form")
