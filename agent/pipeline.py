"""One command: discover jobs, score their fit, and prepare answers for the best matches.

Preparing a job runs the ordinary dry run: a headless browser fills the form, records every
answer and draft, and never submits. Submitting stays with the candidate (hand-off or assist).
"""

import argparse
import logging
import subprocess
import sys
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from agent.applier.cli import PLATFORM_HOSTS
from agent.applier.greenhouse import load_profile
from agent.applier.review import FitSummary
from agent.dashboard import DEFAULT_PORT, refresh_dashboard
from agent.dashboard import serve as serve_dashboard
from agent.filters import is_ambiguous_location, normalize_location
from agent.main import main as discover
from agent.scorer import FitAssessment, score_job
from agent.settings import (
    PROJECT_ROOT,
    AgentSettings,
    excluded_role_reason,
    load_settings,
    title_in_scope,
)
from agent.tracking import REMOVED, qualification_problem, store_fit
from db.models import Job
from db.session import create_database_engine, create_session_factory, ensure_schema

LOGGER = logging.getLogger(__name__)
PROFILE_PATH = PROJECT_ROOT / "profile" / "profile.yaml"
# Only jobs confirmed to be in North Carolina or US-remote are scored and prepared.
WANTED_LOCATIONS = ("nc", "remote_us")
PREPARE_TIMEOUT_SECONDS = 6 * 60
Scorer = Callable[..., FitAssessment]
Runner = Callable[[list[str]], int]


def remove_out_of_scope_jobs(session: Session, settings: AgentSettings) -> int:
    """Remove found jobs the current settings rule out; return how many.

    That is internships, co-ops, new-grad roles, titles outside the wanted roles
    (title_keywords and title_role_keywords), and locations outside North Carolina or US
    remote. Applied, prepared, and already removed jobs are left alone. Removed jobs are
    hidden, and discovery does not add them back.
    """
    jobs = session.scalars(
        select(Job).where(Job.status.in_(("new", "queued", "skipped")))
    ).all()
    removed = 0
    for job in jobs:
        reason = excluded_role_reason(job.title, job.description, settings)
        if reason is None and not title_in_scope(job.title, settings):
            reason = "Not an AI or agent engineering role"
        if (
            reason is None
            and normalize_location(job.location_raw, include_hybrid_nc=settings.include_hybrid_nc)
            == "other"
            and not is_ambiguous_location(job.location_raw)
        ):
            reason = "Not in North Carolina or remote in the US"
        if reason is None:
            continue
        job.status = REMOVED
        job.dealbreakers = list(dict.fromkeys([reason, *(job.dealbreakers or [])]))
        removed += 1
        print(f"[removed] {job.company} | {job.title} | {reason}")
    session.commit()
    return removed


def score_unscored_jobs(
    session: Session,
    profile: Mapping[str, Any],
    settings: AgentSettings,
    *,
    limit: int,
    scorer: Scorer = score_job,
    workers: int = 4,
) -> int:
    """Score jobs that have a description but no fit score; return how many were scored.

    A new job whose fit check finds unmet requirements or a low score is moved to Skipped,
    exactly as job-apply would before filling it.
    """
    jobs = session.scalars(
        select(Job)
        .where(
            Job.fit_score.is_(None),
            Job.status == "new",
            Job.location_category.in_(WANTED_LOCATIONS),
            Job.description != "",
        )
        .order_by(Job.first_seen_at.desc(), Job.id)
        .limit(limit)
    ).all()
    if not jobs:
        return 0
    LOGGER.info("Scoring fit for %d jobs", len(jobs))

    def assess(description: str) -> FitAssessment:
        return scorer(
            description,
            profile,
            fit_score_threshold=settings.fit_score_threshold,
            model=settings.anthropic_model,
        )

    scored = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [(job, pool.submit(assess, job.description)) for job in jobs]
        for job, future in futures:
            try:
                assessment = future.result()
            except Exception as error:
                LOGGER.warning("Could not score %s - %s: %s", job.company, job.title, error)
                continue
            fit = FitSummary(
                score=assessment.score,
                reasons=assessment.reasons,
                dealbreakers=assessment.dealbreakers,
                recommended_action=assessment.recommended_action,
            )
            store_fit(job, fit)
            if job.status == "new" and qualification_problem(fit):
                job.status = "skipped"
            session.commit()
            scored += 1
            print(f"[fit {assessment.score:>3}] {job.company} | {job.title}")
    return scored


def jobs_to_prepare(session: Session, settings: AgentSettings, *, limit: int) -> list[Job]:
    """Best-matching new jobs that pass the fit check and have no answers prepared yet."""
    jobs = session.scalars(
        select(Job)
        .options(selectinload(Job.applications))
        .where(
            Job.status == "new",
            Job.location_category.in_(WANTED_LOCATIONS),
            Job.fit_score >= settings.fit_score_threshold,
            Job.platform.in_(PLATFORM_HOSTS),
        )
        .order_by(Job.fit_score.desc(), Job.first_seen_at.desc())
    ).all()
    ready = [
        job
        for job in jobs
        if not job.applications
        and not job.dealbreakers
        and job.fit_recommendation in (None, "apply")
        and excluded_role_reason(job.title, job.description, settings) is None
        and title_in_scope(job.title, settings)
    ]
    return ready[:limit]


def prepare_command(job: Job) -> list[str]:
    """The dry-run command that fills a job's form headlessly and records every answer."""
    return [
        sys.executable,
        "-m",
        "agent.applier.cli",
        "--platform",
        job.platform,
        "--job-url",
        job.url,
    ]


def run_prepare(command: list[str]) -> int:
    """Run one dry run in its own process so a browser crash cannot stop the batch."""
    try:
        return subprocess.run(
            command, cwd=PROJECT_ROOT, timeout=PREPARE_TIMEOUT_SECONDS, check=False
        ).returncode
    except subprocess.TimeoutExpired:
        LOGGER.warning("Dry run timed out: %s", command[-1])
        return 124


def prepare_jobs(jobs: list[Job], runner: Runner = run_prepare) -> dict[str, int]:
    """Fill each job's form in a dry run; return each job URL's exit code."""
    outcomes: dict[str, int] = {}
    for index, job in enumerate(jobs, start=1):
        print(f"\nPreparing {index}/{len(jobs)}: {job.company} - {job.title} (fit {job.fit_score})")
        outcomes[job.url] = runner(prepare_command(job))
    return outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover jobs, score fit, prepare answers for the best matches, and open the "
            "dashboard. Never submits an application."
        )
    )
    parser.add_argument("--skip-discovery", action="store_true", help="Use jobs already found")
    parser.add_argument(
        "--no-new-grad-list",
        action="store_true",
        help="Only search configured companies, not the public new-grad list.",
    )
    parser.add_argument(
        "--score-limit",
        type=int,
        default=150,
        help="Most jobs to fit-score in this run (each is one Anthropic call; default 150).",
    )
    parser.add_argument(
        "--prepare",
        type=int,
        default=5,
        metavar="N",
        help="Fill answers for the N best new matches in headless dry runs (default 5; 0 skips).",
    )
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    parser.add_argument("--no-open", action="store_true", help="Do not open the dashboard")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run discovery, scoring, and preparation, then refresh and open the dashboard."""
    args = build_parser().parse_args(argv)
    if not args.skip_discovery:
        discover(
            ["--no-dashboard"] + (["--no-new-grad-list"] if args.no_new_grad_list else [])
        )
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = load_settings()
    profile = load_profile(args.profile)
    engine = create_database_engine(settings.database_url)
    try:
        ensure_schema(engine)
        with create_session_factory(engine)() as session:
            removed = remove_out_of_scope_jobs(session, settings)
            if removed:
                print(f"Removed {removed} jobs outside your target roles.")
            scored = score_unscored_jobs(session, profile, settings, limit=args.score_limit)
            print(f"\nScored {scored} jobs.")
            to_prepare = (
                jobs_to_prepare(session, settings, limit=args.prepare) if args.prepare > 0 else []
            )
    finally:
        engine.dispose()

    if args.prepare > 0 and not to_prepare:
        print("No new jobs at or above the fit threshold need answers prepared.")
    outcomes = prepare_jobs(to_prepare)
    if outcomes:
        ready = sum(code in (0, 2) for code in outcomes.values())
        print(f"\nPrepared answers for {ready} of {len(outcomes)} jobs.")

    if not args.no_open:
        return serve_dashboard(DEFAULT_PORT, open_browser=True)
    dashboard = refresh_dashboard()
    if dashboard is not None:
        print(f"Dashboard: {dashboard}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
