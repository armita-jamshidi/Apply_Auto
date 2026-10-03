"""Record application attempts and job statuses so the dashboard can show progress."""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.applier.greenhouse import ApplierResult
from agent.applier.review import FitSummary
from agent.types import JobListing
from db.models import Application, Job, utc_now

READY_FOR_YOU = "manual_review"
# Removed jobs stay in the database (hidden) so discovery does not add them back.
REMOVED = "removed"
MARKABLE_STATUSES = frozenset({"new", "queued", READY_FOR_YOU, "applied", "skipped", REMOVED})


def qualification_problem(fit: FitSummary) -> str | None:
    """Explain why a job should not be filled, or None when the fit check allows it."""
    if fit.score is None:
        return None
    if fit.dealbreakers:
        return "unmet requirements: " + "; ".join(fit.dealbreakers)
    if fit.recommended_action == "skip":
        reason = fit.reasons[0] if fit.reasons else "the fit check recommends skipping it"
        return f"fit score {fit.score} is too low: {reason}"
    return None


def store_fit(job: Job, fit: FitSummary) -> None:
    """Save a fit score, its reasons, dealbreakers, and recommendation on the job."""
    job.fit_score = fit.score
    job.fit_reasons = list(fit.reasons)
    job.dealbreakers = list(fit.dealbreakers)
    if fit.recommended_action is not None:
        job.fit_recommendation = fit.recommended_action


def record_attempt(
    session: Session,
    *,
    job: JobListing,
    lookup_url: str,
    mode: str,
    result: ApplierResult | None,
    fit: FitSummary | None,
    review_path: Path | None,
    skipped_reason: str | None = None,
) -> Job:
    """Store a dry-run or hand-off attempt, creating the job when discovery never saw it."""
    stored = session.scalar(select(Job).where(Job.url == lookup_url))
    description = (result.job_description if result else "") or job.description
    if stored is None:
        stored = Job(
            source=job.source,
            platform=job.platform,
            company=job.company,
            title=job.title,
            url=lookup_url,
            location_raw=job.location_raw,
            location_category="unknown",
            description=description,
            status="new",
        )
        session.add(stored)
        session.flush()
    elif not stored.description and description:
        stored.description = description
    if fit is not None and fit.score is not None:
        store_fit(stored, fit)
    if stored.status != "applied":
        stored.status = "skipped" if skipped_reason else READY_FOR_YOU
    session.add(
        Application(
            job_id=stored.id,
            mode=mode,
            answers=dict(result.answers) if result else {},
            suggested_answers=dict(result.suggested_answers) if result else None,
            field_notes=dict(result.field_notes) if result else None,
            screenshot_path=result.screenshot_path if result else None,
            review_path=str(review_path) if review_path else None,
            error=skipped_reason or (result.error if result else None),
        )
    )
    return stored


def mark_job(session: Session, url: str, status: str) -> Job:
    """Set a job's status by hand; marking it applied records the submission time."""
    if status not in MARKABLE_STATUSES:
        raise ValueError(f"Status must be one of {sorted(MARKABLE_STATUSES)}")
    job = session.scalar(select(Job).where(Job.url == url))
    if job is None:
        raise ValueError(f"No job with URL {url} is in the database")
    job.status = status
    if status == "applied":
        latest = session.scalar(
            select(Application)
            .where(Application.job_id == job.id, Application.mode != "live")
            .order_by(Application.started_at.desc(), Application.id.desc())
            .limit(1)
        )
        if latest is None or latest.submitted_at is not None:
            latest = Application(job_id=job.id, mode="manual", answers={})
            session.add(latest)
        latest.submitted_at = utc_now()
    return job
