"""Tests for application tracking, the qualification gate, and the local dashboard."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from agent import dashboard
from agent.applier import cli
from agent.applier.greenhouse import ApplierResult
from agent.applier.review import FitSummary
from agent.tracking import mark_job, qualification_problem, record_attempt
from agent.types import JobListing
from db.models import Application, Base, Job

JOB = JobListing(
    source="lever",
    platform="lever",
    company="Sample Co",
    title="Software Engineer",
    url="https://jobs.lever.co/sample/job-1/apply",
    location_raw="Raleigh, NC",
    description="Build Python services.",
)
OVERVIEW = "https://jobs.lever.co/sample/job-1"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def add_job(db: Session, url: str, status: str = "new", **fields) -> Job:
    values = {
        "source": "greenhouse",
        "platform": "greenhouse",
        "company": "Example",
        "title": "Engineer",
        "url": url,
        "location_raw": "Remote - US",
        "location_category": "remote_us",
        "description": "Build tools.",
        "status": status,
        **fields,
    }
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


def result() -> ApplierResult:
    return ApplierResult(
        "manual_review",
        {"Email": "sample@example.com"},
        "shot.png",
        None,
        job_description="Build Python services.",
    )


@pytest.mark.parametrize(
    ("fit", "expected"),
    [
        (FitSummary(score=None, note="Not scored"), None),
        (FitSummary(score=85, recommended_action="apply"), None),
        (FitSummary(score=90, dealbreakers=["Requires Spanish"]), "unmet requirements: "),
        (FitSummary(score=40, reasons=["Needs 5+ years"], recommended_action="skip"), "too low"),
    ],
)
def test_qualification_problem(fit: FitSummary, expected: str | None) -> None:
    problem = qualification_problem(fit)
    assert (problem is None) if expected is None else expected in (problem or "")


def test_record_attempt_creates_job_stores_fit_and_marks_ready(session: Session) -> None:
    job = record_attempt(
        session,
        job=JOB,
        lookup_url=OVERVIEW,
        mode="hand_off",
        result=result(),
        fit=FitSummary(score=81, reasons=["Python match"], dealbreakers=[]),
        review_path=Path("reviews/sample.html"),
    )
    session.flush()

    assert job.url == OVERVIEW
    assert job.status == "manual_review"
    assert job.fit_score == 81
    attempt = session.scalar(select(Application).where(Application.job_id == job.id))
    assert attempt.mode == "hand_off"
    assert attempt.review_path == str(Path("reviews/sample.html"))
    assert attempt.submitted_at is None


def test_skipped_attempt_records_reason_but_never_overrides_applied(session: Session) -> None:
    applied = add_job(session, OVERVIEW, status="applied")

    record_attempt(
        session,
        job=JOB,
        lookup_url=OVERVIEW,
        mode="dry_run",
        result=None,
        fit=None,
        review_path=None,
        skipped_reason="unmet requirements: Spanish",
    )

    assert applied.status == "applied"


def test_mark_job_applied_stamps_latest_attempt_or_creates_one(session: Session) -> None:
    job = add_job(session, OVERVIEW)
    session.add(Application(job_id=job.id, mode="hand_off", answers={}))
    session.flush()

    mark_job(session, OVERVIEW, "applied")
    session.flush()

    attempts = session.scalars(select(Application).where(Application.job_id == job.id)).all()
    assert job.status == "applied"
    assert [(item.mode, item.submitted_at is not None) for item in attempts] == [
        ("hand_off", True)
    ]

    other = add_job(session, "https://example.com/other")
    mark_job(session, other.url, "applied")
    session.flush()
    manual = session.scalar(select(Application).where(Application.job_id == other.id))
    assert manual.mode == "manual" and manual.submitted_at is not None


def test_mark_job_rejects_unknown_jobs_and_statuses(session: Session) -> None:
    add_job(session, OVERVIEW)

    with pytest.raises(ValueError, match="No job"):
        mark_job(session, "https://example.com/missing", "applied")
    with pytest.raises(ValueError, match="Status must be"):
        mark_job(session, OVERVIEW, "interviewing")


def test_dashboard_orders_groups_and_links_next_steps(session: Session, tmp_path: Path) -> None:
    review = tmp_path / "review.html"
    review.write_text("review", encoding="utf-8")
    ready = add_job(session, "https://example.com/ready", status="manual_review", fit_score=70)
    session.add(Application(job_id=ready.id, mode="hand_off", answers={}, review_path=str(review)))
    add_job(session, "https://example.com/new-low", fit_score=40, company="Low")
    add_job(
        session,
        "https://example.com/new-high",
        fit_score=90,
        company="High",
        dealbreakers=["Requires Spanish"],
    )
    applied = add_job(session, "https://example.com/applied", status="applied")
    session.add(
        Application(
            job_id=applied.id,
            mode="manual",
            answers={},
            submitted_at=datetime(2026, 10, 3, 18, tzinfo=UTC),
        )
    )
    add_job(session, "https://example.com/skipped", status="skipped")
    add_job(session, "https://example.com/queued", status="queued")
    session.flush()

    page = tmp_path / "dashboard.html"
    rows = dashboard.write_dashboard(session, page)

    assert [row.group for row in rows] == [
        "ready",
        "new",
        "new",
        "location",
        "applied",
        "skipped",
    ]
    assert [row.job.company for row in rows if row.group == "new"] == ["High", "Low"]
    html = page.read_text(encoding="utf-8")
    assert review.resolve().as_uri() in html
    assert "Requires Spanish" in html
    assert "Applied Oct 03" in html
    assert "--hand-off --platform greenhouse" in html
    assert "data-group='skipped'" in html


def test_empty_dashboard_explains_how_to_find_jobs(session: Session, tmp_path: Path) -> None:
    page = tmp_path / "dashboard.html"

    assert dashboard.write_dashboard(session, page) == []
    assert "Run discovery" in page.read_text(encoding="utf-8")


def test_dashboard_cli_marks_applied_and_writes_page(tmp_path: Path) -> None:
    from agent.settings import load_settings
    from db.session import create_database_engine, create_session_factory, ensure_schema

    engine = create_database_engine(load_settings().database_url)
    ensure_schema(engine)
    with create_session_factory(engine)() as db:
        add_job(db, OVERVIEW)
        db.commit()

    assert dashboard.main(["--mark-applied", OVERVIEW, "--no-open"]) == 0

    with create_session_factory(engine)() as db:
        assert db.scalar(select(Job.status).where(Job.url == OVERVIEW)) == "applied"
    engine.dispose()
    assert "Applied" in dashboard.DASHBOARD_PATH.read_text(encoding="utf-8")


def test_apply_skips_stored_job_that_fails_qualification_before_opening_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stored = Job(
        company="Sample Co",
        title="Engineer",
        url=OVERVIEW,
        description="Fluent Spanish required.",
        fit_score=88,
        dealbreakers=["Requires Spanish proficiency, which the profile does not list"],
    )
    tracked: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "_load_stored_job", lambda _url: stored)
    monkeypatch.setattr(
        cli, "_track_attempt", lambda *args, **kwargs: tracked.append(kwargs)
    )
    monkeypatch.setattr(
        cli, "sync_playwright", lambda: (_ for _ in ()).throw(AssertionError("no browser"))
    )
    monkeypatch.setattr(
        sys, "argv", ["job-apply", "--hand-off", "--platform", "lever", "--job-url", OVERVIEW]
    )

    assert cli.main() == 3

    assert "unmet requirements" in str(tracked[0]["skipped_reason"])
    assert "--ignore-fit" in capsys.readouterr().out


def test_hand_off_records_submission_when_person_confirms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakePage:
        def wait_for_event(self, _name: str, *, timeout: int) -> None:
            return None

    class FakePlaywright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace())

        def __exit__(self, *_exc) -> None:
            return None

    tracked: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "sync_playwright", FakePlaywright)
    monkeypatch.setattr(
        cli,
        "launch_hand_off_browser",
        lambda *_args: SimpleNamespace(pages=[FakePage()], close=lambda: None),
    )
    monkeypatch.setitem(cli.APPLIERS, "lever", lambda *_args, **_kwargs: result())
    monkeypatch.setattr(cli, "_load_stored_job", lambda _url: None)
    monkeypatch.setattr(cli, "_dry_run_fit", lambda *_args: FitSummary(score=85))
    monkeypatch.setattr(cli, "_ask_submitted", lambda: True)
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(
        cli, "_track_attempt", lambda *args, **kwargs: tracked.append({"args": args, **kwargs})
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-apply",
            "--hand-off",
            "--platform",
            "lever",
            "--job-url",
            f"{OVERVIEW}/apply",
            "--resume",
            str(tmp_path / "resume.pdf"),
            "--review",
            str(tmp_path / "review.html"),
            "--screenshot",
            str(tmp_path / "shot.png"),
        ],
    )

    cli.main()

    assert tracked[0]["submitted"] is True
    assert tracked[0]["args"][1] == OVERVIEW
    assert tracked[0]["args"][2] == "hand_off"
