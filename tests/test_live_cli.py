"""Tests for recording live application outcomes."""

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent.applier.cli import _persist_live_result
from agent.applier.greenhouse import ApplierResult
from db.models import Application, Base, Job

JOB_URL = "https://jobs.lever.co/sample/job-1"


def create_pending_application() -> tuple[sessionmaker, int]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    with session_factory() as session:
        job = Job(
            source="lever",
            platform="lever",
            company="Sample Co",
            title="Engineer",
            url=JOB_URL,
            location_raw="Remote - US",
            location_category="remote_us",
            description="Python role.",
            fit_score=90,
        )
        session.add(job)
        session.flush()
        application = Application(
            job_id=job.id,
            mode="live",
            answers={},
            started_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        session.add(application)
        session.commit()
        return session_factory, application.id


def test_unknown_live_outcome_is_held_for_manual_check() -> None:
    session_factory, application_id = create_pending_application()
    result = ApplierResult(
        "unknown",
        {"Email": "sample@example.com"},
        "screenshots/filled.png",
        None,
        "Submit was clicked but no confirmation was detected; verify manually.",
    )

    _persist_live_result(session_factory, application_id, JOB_URL, result)

    with session_factory() as session:
        application = session.get(Application, application_id)
        job = application.job
        assert application.submitted_at is None
        assert "verify manually" in (application.error or "")
        assert job.status == "manual_review"


def test_confirmed_live_outcome_marks_job_applied() -> None:
    session_factory, application_id = create_pending_application()
    result = ApplierResult("applied", {}, "screenshots/filled.png", None, submitted=True)

    _persist_live_result(session_factory, application_id, JOB_URL, result)

    with session_factory() as session:
        application = session.get(Application, application_id)
        assert application.submitted_at is not None
        assert application.job.status == "applied"
