"""Tests for live-application safeguards."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent.safeguards import live_application_block_reason
from db.models import Application, Base, Job


def create_job(
    session: Session,
    *,
    score: int | None = 90,
    dealbreakers: list[str] | None = None,
) -> Job:
    job = Job(
        source="greenhouse",
        platform="greenhouse",
        company="Example Co",
        title="Software Engineer",
        url="https://boards.greenhouse.io/example/1",
        location_raw="Remote - US",
        location_category="remote_us",
        description="Build software.",
        fit_score=score,
        dealbreakers=dealbreakers,
    )
    session.add(job)
    session.flush()
    return job


def test_live_guard_allows_scored_job_with_room_in_caps() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)

    with Session(engine) as session:
        job = create_job(session)
        assert live_application_block_reason(
            session,
            job,
            fit_score_threshold=70,
            daily_application_cap=5,
            company_monthly_application_cap=5,
            now=now,
        ) is None

    engine.dispose()


@pytest.mark.parametrize(
    ("score", "dealbreakers", "expected"),
    [
        (None, None, "not been fit-scored"),
        (69, None, "below threshold"),
        (90, ["Requires relocation"], "unresolved dealbreakers"),
    ],
)
def test_live_guard_blocks_unscored_low_score_and_dealbreaker_jobs(
    score: int | None,
    dealbreakers: list[str] | None,
    expected: str,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        job = create_job(session, score=score, dealbreakers=dealbreakers)
        reason = live_application_block_reason(
            session,
            job,
            fit_score_threshold=70,
            daily_application_cap=5,
            company_monthly_application_cap=5,
            now=datetime(2026, 10, 1, tzinfo=UTC),
        )
        assert expected in (reason or "")
    engine.dispose()


def test_live_guard_blocks_out_of_scope_location() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        job = create_job(session)
        job.location_category = "other"
        reason = live_application_block_reason(
            session,
            job,
            fit_score_threshold=70,
            daily_application_cap=5,
            company_monthly_application_cap=5,
            now=datetime(2026, 10, 1, tzinfo=UTC),
        )
        assert "outside the configured application scope" in (reason or "")
    engine.dispose()


@pytest.mark.parametrize("status", ["queued", "manual_review"])
def test_live_guard_blocks_jobs_awaiting_manual_review(status: str) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        job = create_job(session)
        job.location_category = "nc"
        job.status = status
        reason = live_application_block_reason(
            session,
            job,
            fit_score_threshold=70,
            daily_application_cap=5,
            company_monthly_application_cap=5,
            now=datetime(2026, 10, 1, tzinfo=UTC),
        )
        assert f"Job status is {status}" in (reason or "")
    engine.dispose()


def test_live_guard_blocks_duplicate_job_submission() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    with Session(engine) as session:
        job = create_job(session)
        session.add(Application(job_id=job.id, mode="live", answers={}, submitted_at=now))
        session.flush()
        reason = live_application_block_reason(
            session,
            job,
            fit_score_threshold=70,
            daily_application_cap=5,
            company_monthly_application_cap=5,
            now=now,
        )
        assert "reconcile it before retrying" in (reason or "")
    engine.dispose()


@pytest.mark.parametrize("cap", [1, 2])
def test_live_guard_blocks_daily_cap(cap: int) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    with Session(engine) as session:
        job = create_job(session)
        for index in range(cap):
            previous = Job(
                source="greenhouse",
                platform="greenhouse",
                company=f"Other Co {index}",
                title="Engineer",
                url=f"https://boards.greenhouse.io/other/{index}",
                location_raw="Remote - US",
                location_category="remote_us",
                description="",
            )
            session.add(previous)
            session.flush()
            session.add(Application(job_id=previous.id, mode="live", answers={}, started_at=now))
        session.flush()
        reason = live_application_block_reason(
            session,
            job,
            fit_score_threshold=70,
            daily_application_cap=cap,
            company_monthly_application_cap=5,
            now=now,
        )
        assert "Daily live application cap" in (reason or "")
    engine.dispose()


def test_live_guard_blocks_monthly_company_cap_case_insensitively() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    with Session(engine) as session:
        job = create_job(session)
        previous = Job(
            source="greenhouse",
            platform="greenhouse",
            company="EXAMPLE CO",
            title="Engineer",
            url="https://boards.greenhouse.io/example/previous",
            location_raw="Remote - US",
            location_category="remote_us",
            description="",
        )
        session.add(previous)
        session.flush()
        session.add(Application(job_id=previous.id, mode="live", answers={}, started_at=now))
        session.flush()

        reason = live_application_block_reason(
            session,
            job,
            fit_score_threshold=70,
            daily_application_cap=5,
            company_monthly_application_cap=1,
            now=now,
        )

        assert "Monthly live application cap" in (reason or "")
    engine.dispose()


def count_block_with_previous_attempt(
    previous_started_at: datetime,
    now: datetime,
    *,
    daily_cap: int = 1,
    company_cap: int = 5,
    previous_company: str = "Other Co",
) -> str | None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        job = create_job(session)
        previous = Job(
            source="greenhouse",
            platform="greenhouse",
            company=previous_company,
            title="Engineer",
            url="https://boards.greenhouse.io/other/previous",
            location_raw="Remote - US",
            location_category="remote_us",
            description="",
        )
        session.add(previous)
        session.flush()
        session.add(
            Application(
                job_id=previous.id, mode="live", answers={}, started_at=previous_started_at
            )
        )
        session.flush()
        reason = live_application_block_reason(
            session,
            job,
            fit_score_threshold=70,
            daily_application_cap=daily_cap,
            company_monthly_application_cap=company_cap,
            now=now,
        )
    engine.dispose()
    return reason


def test_daily_cap_uses_new_york_day_not_utc_day() -> None:
    # 14:00 UTC and 02:00 UTC next day are both October 1 in New York (EDT).
    reason = count_block_with_previous_attempt(
        datetime(2026, 10, 1, 14, tzinfo=UTC),
        datetime(2026, 10, 2, 2, tzinfo=UTC),
    )
    assert "Daily live application cap" in (reason or "")


def test_daily_cap_resets_at_new_york_midnight() -> None:
    # 03:30 UTC is 23:30 EDT on October 1; 04:30 UTC is 00:30 EDT on October 2.
    reason = count_block_with_previous_attempt(
        datetime(2026, 10, 2, 3, 30, tzinfo=UTC),
        datetime(2026, 10, 2, 4, 30, tzinfo=UTC),
    )
    assert reason is None


def test_daily_cap_window_spans_dst_change() -> None:
    # November 1, 2026 is 25 hours long in New York; 04:30 UTC on Nov 2 is still Nov 1 EST.
    reason = count_block_with_previous_attempt(
        datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
        datetime(2026, 11, 2, 4, 30, tzinfo=UTC),
    )
    assert "Daily live application cap" in (reason or "")


def test_monthly_company_cap_uses_new_york_month() -> None:
    # 02:00 UTC on November 1 is still October 31 in New York.
    reason = count_block_with_previous_attempt(
        datetime(2026, 10, 15, 12, tzinfo=UTC),
        datetime(2026, 11, 1, 2, tzinfo=UTC),
        daily_cap=5,
        company_cap=1,
        previous_company="Example Co",
    )
    assert "Monthly live application cap" in (reason or "")
