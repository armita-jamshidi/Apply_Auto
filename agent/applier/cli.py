"""Routed Tier 1 application CLI with guarded, explicit live mode."""

import argparse
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agent.applier.ashby import run_ashby_application
from agent.applier.base import application_urls
from agent.applier.greenhouse import (
    ApplierResult,
    load_profile,
    run_greenhouse_dry_run,
)
from agent.applier.lever import run_lever_application
from agent.applier.review import FitSummary, count_statuses, write_review_page
from agent.applier.smartrecruiters import run_smartrecruiters_application
from agent.safeguards import live_application_block_reason
from agent.scorer import score_job
from agent.settings import PROJECT_ROOT, load_settings
from agent.types import JobListing
from db.models import Application, Job, utc_now
from db.session import create_database_engine, create_session_factory

LOGGER = logging.getLogger(__name__)
PLATFORM_HOSTS = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("jobs.lever.co", "jobs.eu.lever.co"),
    "ashby": ("jobs.ashbyhq.com",),
    "smartrecruiters": ("jobs.smartrecruiters.com",),
}
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
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Submit only if persisted score, dealbreaker, duplicate, and rate-limit "
            "guards pass."
        ),
    )
    return parser


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
        lookup_url = args.job_url
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

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            try:
                page = browser.new_page()
                kwargs = {
                    "fill_reviewed_motivation_drafts": args.fill_reviewed_motivation_drafts,
                    "submit_live": args.live,
                }
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
            finally:
                browser.close()
        if args.live:
            assert session_factory is not None
            assert application_id is not None
            _persist_live_result(session_factory, application_id, args.job_url, result)
    finally:
        if engine is not None:
            engine.dispose()

    if not args.live:
        fit = _dry_run_fit(stored, result.job_description or description, args.profile)
        rows = write_review_page(
            review_path, job=job, result=result, fit=fit, resume_name=args.resume.name
        )
        counts = ", ".join(
            f"{count} {status}" for status, count in count_statuses(rows).items() if count
        )
        print(f"Review page: {review_path}")
        print(f"Fields: {counts}")
        print(f"Fit score: {fit.score if fit.score is not None else fit.note}")

    print(f"Status: {result.status}")
    print(f"Mode: {'live' if args.live else 'dry_run'}")
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
