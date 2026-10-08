"""Routed Tier 1 application CLI with guarded, explicit live mode."""

import argparse
import logging
import os
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agent.applier.ashby import run_ashby_application
from agent.applier.base import application_urls
from agent.applier.browser_profile import (
    BROWSER_PROFILE_DIR,
    PROFILE_IN_USE_MESSAGE,
    browser_profile_in_use,
)
from agent.applier.greenhouse import (
    ApplierResult,
    load_profile,
    run_greenhouse_dry_run,
)
from agent.applier.lever import run_lever_application
from agent.applier.review import (
    FitSummary,
    count_statuses,
    hand_off_command,
    profile_quick_answers,
    write_review_page,
)
from agent.applier.smartrecruiters import run_smartrecruiters_application
from agent.dashboard import refresh_dashboard
from agent.safeguards import live_application_block_reason
from agent.scorer import score_job
from agent.settings import PROJECT_ROOT, load_settings
from agent.tracking import mark_job, qualification_problem, record_attempt
from agent.types import FILLABLE_HOSTS, JobListing
from db.models import Application, Job, utc_now
from db.session import create_database_engine, create_session_factory, ensure_schema

LOGGER = logging.getLogger(__name__)
PLATFORM_HOSTS = FILLABLE_HOSTS
# Hand-off browser profile: keeps the logins people make in that window between runs.
HUMAN_CHALLENGE_WAIT_MS = 10 * 60 * 1000
# Everyday Chrome profiles: automation must not use them (Chrome blocks it, and they hold
# every login the person has).
DEFAULT_CHROME_PROFILES = (
    Path(os.environ.get("LOCALAPPDATA", "~/AppData/Local")) / "Google" / "Chrome" / "User Data",
    Path("~/Library/Application Support/Google/Chrome"),
    Path("~/.config/google-chrome"),
)
APPLIERS = {
    "lever": run_lever_application,
    "ashby": run_ashby_application,
    "smartrecruiters": run_smartrecruiters_application,
}


def default_resume_path() -> Path:
    """Return RESUME_PATH from the environment or local .env, else profile/resume.pdf."""
    load_dotenv(PROJECT_ROOT / ".env")
    configured = os.getenv("RESUME_PATH", "").strip()
    if not configured:
        return PROJECT_ROOT / "profile" / "resume.pdf"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def build_parser() -> argparse.ArgumentParser:
    """Build arguments for default dry-run or explicitly guarded live application."""
    parser = argparse.ArgumentParser(
        description="Fill a Tier 1 application. Dry-run is default; --live is guarded."
    )
    parser.add_argument("--job-url", required=True, help="HTTPS job application URL")
    parser.add_argument("--platform", choices=sorted(PLATFORM_HOSTS), default="greenhouse")
    parser.add_argument("--profile", type=Path, default=PROJECT_ROOT / "profile" / "profile.yaml")
    parser.add_argument(
        "--resume",
        type=Path,
        default=default_resume_path(),
        help="Resume PDF; defaults to RESUME_PATH from .env, else profile/resume.pdf",
    )
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument(
        "--review",
        type=Path,
        help="Dry-run HTML review page path; defaults to reviews/<platform>-<time>.html",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window; the default is headless.",
    )
    parser.add_argument("--company")
    parser.add_argument("--title")
    parser.add_argument(
        "--fill-reviewed-motivation-drafts",
        action="store_true",
        help="Fill an evidence-validated motivation draft; review remains required.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--live",
        action="store_true",
        help=(
            "Submit only if persisted score, dealbreaker, duplicate, and rate-limit "
            "guards pass."
        ),
    )
    parser.add_argument(
        "--ignore-fit",
        action="store_true",
        help="Fill the form even when the fit check finds unmet requirements or a low score.",
    )
    parser.add_argument(
        "--browser",
        choices=("chrome", "chromium"),
        default="chrome",
        help=(
            "Browser for --hand-off: your installed Google Chrome (default) or Playwright's "
            "bundled Chromium."
        ),
    )
    parser.add_argument(
        "--browser-profile",
        type=Path,
        help="Profile folder for --hand-off (default: .playwright/profile in the project).",
    )
    mode.add_argument(
        "--assist",
        action="store_true",
        help=(
            "Work out every answer in a hidden browser, then open the untouched form in your "
            "own browser with a review page of answers to copy. Use for sites that reject "
            "automated browsers. Never submits."
        ),
    )
    mode.add_argument(
        "--hand-off",
        action="store_true",
        help=(
            "Fill the form in a visible browser and leave it open for you to log in, "
            "finish, and submit yourself. Never submits."
        ),
    )
    return parser


def launch_hand_off_browser(
    playwright: Playwright, browser: str, profile_dir: Path | None
) -> BrowserContext:
    """Open a visible, persistent browser for hand-off, preferring the installed Chrome.

    no_viewport lets the page fit the window. Playwright otherwise draws every page at a
    fixed 1280x720, so in a smaller window the bottom of the form is cut off and cannot be
    scrolled to.
    """
    profile = (profile_dir or BROWSER_PROFILE_DIR).expanduser().resolve()
    if is_everyday_chrome_profile(profile):
        raise SystemExit(
            "--browser-profile must not be your everyday Chrome profile; Chrome blocks "
            "automation there and it holds all your logins. Use a separate folder."
        )
    if browser_profile_in_use(profile):
        raise SystemExit(PROFILE_IN_USE_MESSAGE)
    profile.mkdir(parents=True, exist_ok=True)
    if browser == "chrome":
        try:
            return playwright.chromium.launch_persistent_context(
                str(profile),
                headless=False,
                channel="chrome",
                chromium_sandbox=True,
                no_viewport=True,
            )
        except PlaywrightError as error:
            LOGGER.warning(
                "Could not start installed Google Chrome; using Playwright's Chromium: %s",
                str(error).splitlines()[0],
            )
    # Playwright disables Chrome's sandbox unless asked; keep it on.
    return playwright.chromium.launch_persistent_context(
        str(profile), headless=False, chromium_sandbox=True, no_viewport=True
    )


def is_everyday_chrome_profile(profile: Path) -> bool:
    """Return whether a folder is, or is inside, a default Chrome user-data directory."""
    resolved = profile.expanduser().resolve()
    for default in DEFAULT_CHROME_PROFILES:
        root = default.expanduser().resolve()
        if resolved == root or root in resolved.parents:
            return True
    return False


def finish_command(args: argparse.Namespace, company: str, title: str) -> str:
    """Return the command that reopens this job's form, filled, in hand-off mode."""
    return hand_off_command(args.platform, args.job_url, company, title)


def form_url(platform: str, job_url: str) -> str:
    """Best link to the application form; SmartRecruiters' is only known from its page."""
    if platform == "greenhouse":
        return job_url
    return application_urls(platform, job_url)[1] or job_url


def _validate_job_url(url: str, platform: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    hosts = PLATFORM_HOSTS[platform]
    if parsed.scheme != "https" or not any(
        hostname == host or hostname.endswith(f".{host}") for host in hosts
    ):
        raise SystemExit(f"--job-url must be an HTTPS {platform} URL")


def _load_stored_job(job_url: str) -> Job | None:
    """Return the discovered job for a dry run, or None when it is unavailable."""
    try:
        settings = load_settings()
        engine = create_database_engine(settings.database_url)
    except (ValueError, SQLAlchemyError, ImportError) as error:
        LOGGER.info("Database unavailable for dry run; using page details only: %s", error)
        return None
    try:
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            job = session.scalar(select(Job).where(Job.url == job_url))
            if job is not None:
                session.expunge(job)
            return job
    except SQLAlchemyError as error:
        LOGGER.info("Database unavailable for dry run; using page details only: %s", error)
        return None
    finally:
        engine.dispose()


def _dry_run_fit(stored: Job | None, description: str, profile_path: Path) -> FitSummary:
    """Use a stored fit score when present; otherwise score the description if available."""
    if stored is not None and stored.fit_score is not None:
        return FitSummary(
            score=stored.fit_score,
            reasons=list(stored.fit_reasons or []),
            dealbreakers=list(stored.dealbreakers or []),
            note="Stored score from the database.",
        )
    if not description.strip():
        return FitSummary(note="Not scored: no job description was available.")
    try:
        settings = load_settings()
        assessment = score_job(
            description,
            load_profile(profile_path),
            fit_score_threshold=settings.fit_score_threshold,
            model=settings.anthropic_model,
            job_posting=(
                {"title": stored.title, "company": stored.company, "location": stored.location_raw}
                if stored is not None
                else None
            ),
        )
    except Exception as error:
        LOGGER.warning("Could not score dry-run job: %s", error)
        return FitSummary(note=f"Not scored: {type(error).__name__}: {error}")
    return FitSummary(
        score=assessment.score,
        reasons=assessment.reasons,
        dealbreakers=assessment.dealbreakers,
        recommended_action=assessment.recommended_action,
    )


def _report_dry_run(
    args: argparse.Namespace,
    job: JobListing,
    result: ApplierResult,
    stored: Job | None,
    description: str,
    review_path: Path,
    fit: FitSummary | None = None,
) -> FitSummary:
    if fit is None:
        fit = _dry_run_fit(stored, result.job_description or description, args.profile)
    rows = write_review_page(
        review_path,
        job=job,
        result=result,
        fit=fit,
        resume_name=args.resume.name,
        resume_file=str(args.resume.resolve()),
        assist_command=hand_off_command(
            args.platform, args.job_url, job.company, job.title, mode="assist"
        ),
        form_url=form_url(args.platform, args.job_url),
        finish_command=finish_command(args, job.company, job.title),
        quick_answers=_quick_answers(args.profile),
    )
    counts = ", ".join(
        f"{count} {status}" for status, count in count_statuses(rows).items() if count
    )
    print(f"Review page: {review_path}")
    print(f"Fields: {counts}")
    print(f"Fit score: {fit.score if fit.score is not None else fit.note}")
    return fit


def _quick_answers(profile_path: Path) -> list[tuple[str, str]]:
    """Profile answers for the review page; empty when the profile cannot be read."""
    try:
        return profile_quick_answers(load_profile(profile_path))
    except (OSError, ValueError) as error:
        LOGGER.info("Review page without profile details: %s", error)
        return []


def _ask_submitted() -> bool:
    """Ask in the terminal whether the person submitted; False when no one can answer."""
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    try:
        answer = input("\nDid you submit this application? [y/N] ")
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _track_attempt(
    job: JobListing,
    lookup_url: str,
    mode: str,
    result: ApplierResult | None,
    fit: FitSummary | None,
    review_path: Path | None,
    *,
    skipped_reason: str | None = None,
    submitted: bool = False,
) -> None:
    """Record the attempt for the dashboard; a missing database only logs a note."""
    try:
        engine = create_database_engine(load_settings().database_url)
    except (ValueError, SQLAlchemyError, ImportError) as error:
        LOGGER.info("Attempt not recorded; database unavailable: %s", error)
        return
    try:
        ensure_schema(engine)
        with create_session_factory(engine)() as session:
            record_attempt(
                session,
                job=job,
                lookup_url=lookup_url,
                mode=mode,
                result=result,
                fit=fit,
                review_path=review_path,
                skipped_reason=skipped_reason,
            )
            session.flush()
            if submitted:
                mark_job(session, lookup_url, "applied")
            session.commit()
    except SQLAlchemyError as error:
        LOGGER.info("Attempt not recorded; database unavailable: %s", error)
    finally:
        engine.dispose()


def _prepare_live_application(
    job_url: str,
    platform: str,
    profile_path: Path,
) -> tuple[Engine, sessionmaker[Session], int, str, str, str]:
    settings = load_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            job = session.scalar(select(Job).where(Job.url == job_url).with_for_update())
            if job is None:
                raise ValueError("Job URL is not in the database; run discovery before live mode.")
            if job.platform != platform:
                raise ValueError(
                    f"Configured platform is {job.platform}, not requested platform {platform}."
                )

            profile = load_profile(profile_path)
            if not job.description.strip():
                raise ValueError("Job has no stored description and cannot be fit-scored safely.")
            assessment = score_job(
                job.description,
                profile,
                fit_score_threshold=settings.fit_score_threshold,
                model=settings.anthropic_model,
                job_posting={
                    "title": job.title,
                    "company": job.company,
                    "location": job.location_raw,
                },
            )
            job.fit_score = assessment.score
            job.fit_reasons = assessment.reasons
            job.dealbreakers = assessment.dealbreakers
            session.flush()

            if assessment.recommended_action != "apply":
                raise ValueError(
                    "Fit scorer did not recommend apply "
                    f"(score={assessment.score}, action={assessment.recommended_action})."
                )
            reason = live_application_block_reason(
                session,
                job,
                fit_score_threshold=settings.fit_score_threshold,
                daily_application_cap=settings.daily_application_cap,
                company_monthly_application_cap=settings.company_monthly_application_cap,
            )
            if reason:
                raise ValueError(reason)

            application = Application(job_id=job.id, mode="live", answers={})
            session.add(application)
            session.flush()
            application_id = application.id
            session.commit()
            company = job.company
            title = job.title
            job_description = job.description
    except Exception:
        engine.dispose()
        raise
    return engine, session_factory, application_id, company, title, job_description


def _persist_live_result(
    session_factory: sessionmaker[Session],
    application_id: int,
    job_url: str,
    result: ApplierResult,
) -> None:
    with session_factory() as session:
        application = session.get(Application, application_id)
        job = session.scalar(select(Job).where(Job.url == job_url))
        if application is None or job is None:
            raise RuntimeError("Pending live application record disappeared")
        application.answers = result.answers
        application.screenshot_path = result.screenshot_path
        application.error = result.error
        if result.submitted:
            application.submitted_at = utc_now()
            job.status = "applied"
        elif result.status == "unknown":
            job.status = "manual_review"
            LOGGER.warning(
                "Live submission outcome unknown for %s; check it manually (application %d).",
                job_url,
                application_id,
            )
        elif application.error is None:
            application.error = f"Not submitted; applier status: {result.status}"
        session.commit()


def main() -> int:
    """Run the selected adapter, keeping submission opt-in and safeguard-gated."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    _validate_job_url(args.job_url, args.platform)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    screenshot_path = args.screenshot or (
        PROJECT_ROOT / "screenshots" / f"{args.platform}-{stamp}.png"
    )
    review_path = args.review or PROJECT_ROOT / "reviews" / f"{args.platform}-{stamp}.html"
    mode = next(
        (
            name
            for name, enabled in (
                ("live", args.live),
                ("hand_off", args.hand_off),
                ("assist", args.assist),
            )
            if enabled
        ),
        "dry_run",
    )
    lookup_url = args.job_url
    stored: Job | None = None
    engine: Engine | None = None
    session_factory: sessionmaker[Session] | None = None
    application_id = None
    if args.live:
        try:
            (
                engine,
                session_factory,
                application_id,
                company,
                title,
                description,
            ) = _prepare_live_application(args.job_url, args.platform, args.profile)
        except Exception as error:
            raise SystemExit(f"Live application blocked: {error}") from error
    else:
        # Discovery stores overview pages, so look up /apply-style URLs by their overview.
        if args.platform != "greenhouse":
            lookup_url = application_urls(args.platform, args.job_url)[0] or args.job_url
        stored = _load_stored_job(lookup_url)
        company = args.company or (stored.company if stored else "Unknown company")
        title = args.title or (stored.title if stored else "Unknown role")
        description = stored.description if stored else ""

    job = JobListing(
        source=args.platform,
        platform=args.platform,
        company=company,
        title=title,
        url=args.job_url,
        location_raw="",
        description=description,
    )

    early_fit: FitSummary | None = None
    if not args.live and not args.ignore_fit and stored is not None and stored.description.strip():
        early_fit = _dry_run_fit(stored, stored.description, args.profile)
        problem = qualification_problem(early_fit)
        if problem:
            _track_attempt(job, lookup_url, mode, None, early_fit, None, skipped_reason=problem)
            refresh_dashboard()
            print(
                f"Not filling {company} - {title}: {problem}\n"
                "Run again with --ignore-fit to fill it anyway."
            )
            return 3

    reported = False
    fit: FitSummary | None = early_fit
    skip_reason: str | None = None
    submitted_by_person = False
    try:
        with sync_playwright() as playwright:
            if args.hand_off:
                browser = launch_hand_off_browser(playwright, args.browser, args.browser_profile)
                page = browser.pages[0] if browser.pages else browser.new_page()
            else:
                browser = playwright.chromium.launch(
                    headless=args.assist or not args.headed, chromium_sandbox=True
                )
                page = browser.new_page()
            try:
                kwargs: dict[str, object] = {
                    # In hand-off the person reviews the filled form and submits it, so
                    # written drafts are typed in for them to check there.
                    "fill_reviewed_motivation_drafts": (
                        args.fill_reviewed_motivation_drafts or args.hand_off
                    ),
                    "submit_live": args.live,
                }
                if args.hand_off and args.platform != "greenhouse":
                    kwargs["human_challenge_wait_ms"] = HUMAN_CHALLENGE_WAIT_MS
                if args.platform == "greenhouse":
                    result = run_greenhouse_dry_run(
                        page,
                        job,
                        args.profile,
                        args.resume,
                        screenshot_path,
                        **kwargs,
                    )
                else:
                    result = APPLIERS[args.platform](
                        page,
                        job,
                        args.profile,
                        args.resume,
                        screenshot_path,
                        **kwargs,
                    )
                if args.hand_off:
                    fit = _report_dry_run(
                        args, job, result, stored, description, review_path, early_fit
                    )
                    reported = True
                    skip_reason = None if args.ignore_fit else qualification_problem(fit)
                if args.hand_off and skip_reason:
                    print(
                        f"\nNot handing this form over: {skip_reason}\n"
                        "Run again with --ignore-fit to finish it anyway."
                    )
                elif args.hand_off:
                    webbrowser.open(review_path.resolve().as_uri())
                    print(
                        "\nThe filled form is open in the browser window. Log in if the site "
                        "asks, check every field against the review page, answer what is "
                        "left, and submit it yourself. Close the window when you are done.\n"
                        "Submit soon after the form fills: CAPTCHA checks expire. If one says "
                        "it expired, close this window and run the same command again for a "
                        "fresh fill (reloading the page clears the answers)."
                    )
                    try:
                        page.wait_for_event("close", timeout=0)
                    except PlaywrightError:
                        LOGGER.info("Hand-off browser closed")
                    submitted_by_person = _ask_submitted()
            finally:
                browser.close()
        if args.live:
            assert session_factory is not None
            assert application_id is not None
            _persist_live_result(session_factory, application_id, args.job_url, result)
    finally:
        if engine is not None:
            engine.dispose()

    if not args.live and not reported:
        fit = _report_dry_run(args, job, result, stored, description, review_path, early_fit)
        skip_reason = None if args.ignore_fit else qualification_problem(fit)
    if args.assist and skip_reason:
        print(
            f"\nNot opening this form: {skip_reason}\n"
            "Run again with --ignore-fit to open it anyway."
        )
    elif args.assist:
        webbrowser.open(form_url(args.platform, args.job_url))
        webbrowser.open(review_path.resolve().as_uri())
        print(
            "\nOpened the application form in your browser (not automated) and the review "
            "page with every answer. Copy each answer into the form, upload the resume file "
            "shown on the review page, and submit it yourself."
        )

    if not args.live:
        _track_attempt(
            job,
            lookup_url,
            mode,
            result,
            fit,
            review_path,
            skipped_reason=skip_reason,
            submitted=submitted_by_person,
        )
    dashboard = refresh_dashboard()
    if submitted_by_person:
        print(f"Recorded {company} - {title} as applied.")
    if dashboard is not None:
        print(f"Dashboard: {dashboard}")

    print(f"Status: {result.status}")
    print(f"Mode: {mode}")
    print(f"Resume uploaded: {'yes' if result.resume_uploaded else 'no'}")
    print(f"Submitted: {'yes' if result.submitted else 'no'}")
    print(f"Screenshot: {result.screenshot_path or 'not captured'}")
    print(f"Recorded answer fields: {len(result.answers)}")
    for question, answer in result.suggested_answers.items():
        print(f"Draft for review [{question}]:\n{answer}")
    if result.error:
        print(f"Error: {result.error}")
    return 0 if result.status in {"dry_run_ready", "applied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
