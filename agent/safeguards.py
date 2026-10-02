"""Fail-closed checks for explicitly live application attempts."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Application, Job

REVIEW_STATUSES = frozenset({"queued", "manual_review"})
# Caps reset at local midnight for the candidate, not at UTC midnight.
CAP_TIMEZONE = ZoneInfo("America/New_York")


def live_application_block_reason(
    session: Session,
    job: Job,
    *,
    fit_score_threshold: int,
    daily_application_cap: int,
    company_monthly_application_cap: int,
    now: datetime | None = None,
) -> str | None:
    """Return a blocking reason unless the persisted job may be submitted live."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if job.id is None:
        return "Job must be persisted before a live application."
    if job.location_category not in {"remote_us", "nc"}:
        return "Job location is outside the configured application scope."
    if job.status == "applied":
        return "Job is already marked applied."
    if job.status in REVIEW_STATUSES:
        return f"Job status is {job.status}; resolve the manual review before a live application."
    if job.fit_score is None:
        return "Job has not been fit-scored."
    if job.fit_score < fit_score_threshold:
        return f"Fit score {job.fit_score} is below threshold {fit_score_threshold}."
    if job.dealbreakers:
        return "Job has unresolved dealbreakers."
    if daily_application_cap < 1 or company_monthly_application_cap < 1:
        return "Application caps must be positive."

    previous_live_attempt = select(Application.id).where(
        Application.job_id == job.id,
        Application.mode == "live",
    )
    if session.scalar(previous_live_attempt.limit(1)) is not None:
        return (
            "A live application attempt already exists for this job; "
            "reconcile it before retrying."
        )

    local_now = current.astimezone(CAP_TIMEZONE)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_count = session.scalar(
        select(func.count(Application.id)).where(
            Application.mode == "live",
            Application.started_at >= _as_utc(day_start),
            Application.started_at < _as_utc(day_start + timedelta(days=1)),
        )
    ) or 0
    if day_count >= daily_application_cap:
        return f"Daily live application cap reached ({daily_application_cap})."

    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = _next_month(month_start)
    company_count = session.scalar(
        select(func.count(Application.id))
        .join(Job, Application.job_id == Job.id)
        .where(
            Application.mode == "live",
            Application.started_at >= _as_utc(month_start),
            Application.started_at < _as_utc(next_month),
            func.lower(Job.company) == job.company.casefold(),
        )
    ) or 0
    if company_count >= company_monthly_application_cap:
        return f"Monthly live application cap reached for {job.company}."

    return None


def _as_utc(value: datetime) -> datetime:
    # SQLite drops UTC offsets on storage, so compare against UTC like utc_now() writes.
    return value.astimezone(UTC)


def _next_month(month_start: datetime) -> datetime:
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1)
    return month_start.replace(month=month_start.month + 1)
