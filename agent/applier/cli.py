"""Routed Tier 1 application CLI with guarded, explicit live mode."""

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from agent.applier.ashby import run_ashby_application
from agent.applier.greenhouse import (
    ApplierResult,
    load_profile,
    run_greenhouse_dry_run,
)
from agent.applier.lever import run_lever_application
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
        default=PROJECT_ROOT / "profile" / "resume.pdf",
    )
    parser.add_argument("--screenshot", type=Path)
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

    screenshot_path = args.screenshot or (
        PROJECT_ROOT
        / "screenshots"
        / f"{args.platform}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.png"
    )
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
        company = args.company or "Unknown company"
        title = args.title or "Unknown role"
        description = ""

    job = JobListing(
        source=args.platform,
        platform=args.platform,
        company=company if args.live else args.company or company,
        title=title if args.live else args.title or title,
        url=args.job_url,
        location_raw="",
        description=description,
    )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
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
