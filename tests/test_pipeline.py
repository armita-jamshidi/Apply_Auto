"""Tests for batch fit scoring, choosing jobs to prepare, and answers on the dashboard."""

from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import dashboard, pipeline
from agent.applier.greenhouse import ApplierResult
from agent.applier.review import FitSummary
from agent.scorer import FitAssessment
from agent.settings import load_settings
from agent.tracking import record_attempt
from agent.types import JobListing
from db.models import Application, Base, Job


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def add_job(db: Session, url: str, **fields) -> Job:
    values = {
        "source": "lever",
        "platform": "lever",
        "company": "Example",
        "title": "AI Engineer",
        "url": url,
        "location_raw": "Remote - US",
        "location_category": "remote_us",
        "description": "Build Python services.",
        "status": "new",
        **fields,
    }
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


def fake_scorer(scores: dict[str, FitAssessment]):
    def score(description: str, _profile, **kwargs) -> FitAssessment:
        assert kwargs["job_posting"]["location"] == "Remote - US"
        if description not in scores:
            raise RuntimeError("API unavailable")
        return scores[description]

    return score


def test_scoring_stores_fit_and_skips_jobs_that_fail_the_check(session: Session) -> None:
    strong = add_job(session, "https://jobs.lever.co/a/1", description="strong")
    weak = add_job(session, "https://jobs.lever.co/a/2", description="weak")
    failing = add_job(session, "https://jobs.lever.co/a/3", description="broken")
    add_job(session, "https://jobs.lever.co/a/4", description="")
    scorer = fake_scorer(
        {
            "strong": FitAssessment(
                score=88, reasons=["Python match"], dealbreakers=[], recommended_action="apply"
            ),
            "weak": FitAssessment(
                score=30, reasons=["Needs 5+ years"], dealbreakers=[], recommended_action="skip"
            ),
        }
    )

    scored = pipeline.score_unscored_jobs(
        session, {}, load_settings(), limit=10, scorer=scorer, workers=1
    )

    assert scored == 2
    assert (strong.fit_score, strong.fit_recommendation, strong.status) == (88, "apply", "new")
    assert (weak.fit_score, weak.status) == (30, "skipped")
    assert failing.fit_score is None and failing.status == "new"


def test_jobs_to_prepare_picks_best_unattempted_matches(session: Session) -> None:
    settings = load_settings()
    high = add_job(session, "https://jobs.lever.co/a/high", fit_score=95)
    add_job(session, "https://jobs.lever.co/a/mid", fit_score=settings.fit_score_threshold)
    add_job(session, "https://jobs.lever.co/a/low", fit_score=settings.fit_score_threshold - 1)
    add_job(session, "https://jobs.lever.co/a/blocked", fit_score=99, dealbreakers=["Spanish"])
    add_job(session, "https://jobs.lever.co/a/unscored")
    add_job(session, "https://example.com/workday", fit_score=99, platform="workday")
    tried = add_job(session, "https://jobs.lever.co/a/tried", fit_score=99)
    session.add(Application(job_id=tried.id, mode="dry_run", answers={}))
    session.flush()

    chosen = pipeline.jobs_to_prepare(session, settings, limit=5)

    assert [job.url.rsplit("/", 1)[-1] for job in chosen] == ["high", "mid"]
    assert pipeline.jobs_to_prepare(session, settings, limit=1) == [high]


def test_prepare_runs_a_dry_run_per_job_and_never_submits(session: Session) -> None:
    job = add_job(session, "https://jobs.lever.co/a/1", fit_score=90)
    commands: list[list[str]] = []

    outcomes = pipeline.prepare_jobs([job], runner=lambda cmd: commands.append(cmd) or 0)

    assert outcomes == {job.url: 0}
    assert commands[0][-4:] == ["--platform", "lever", "--job-url", job.url]
    assert not {"--live", "--hand-off", "--assist"} & set(commands[0])


def test_company_forms_off_the_supported_boards_are_prepared_by_the_form_agent(
    session: Session,
) -> None:
    settings = load_settings()
    zoho = "https://acme.zohorecruit.com/jobs/Careers/1/AI-Engineer"
    company_form = add_job(
        session,
        "https://himalayas.app/companies/acme/jobs/ai-engineer",
        fit_score=90,
        platform="himalayas",
        apply_url=zoho,
    )
    add_job(
        session,
        "https://himalayas.app/companies/other/jobs/ai-engineer",
        fit_score=95,
        platform="himalayas",
        apply_url="https://himalayas.app/companies/other/jobs/ai-engineer",
    )

    assert pipeline.jobs_to_prepare(session, settings, limit=5) == [company_form]
    command = pipeline.prepare_command(company_form)
    assert command[2:] == [
        "agent.form_agent",
        "--headless",
        "--job-url",
        zoho,
        "--lookup-url",
        company_form.url,
    ]


def test_fresh_postings_are_prepared_first(session: Session) -> None:
    settings = load_settings()
    now = datetime.now(UTC)
    add_job(
        session, "https://jobs.lever.co/a/old", fit_score=99, posted_at=now - timedelta(days=40)
    )
    fresh = add_job(
        session, "https://jobs.lever.co/a/fresh", fit_score=80, posted_at=now - timedelta(days=1)
    )

    assert pipeline.jobs_to_prepare(session, settings, limit=1) == [fresh]


def test_dashboard_shows_match_reasons_and_every_answer(session: Session, tmp_path: Path) -> None:
    listing = JobListing(
        source="lever",
        platform="lever",
        company="Rocket Co",
        title="Software Engineer",
        url="https://jobs.lever.co/rocket/1",
        location_raw="Remote - US",
        description="Build flight software.",
    )
    accomplishments = "Please summarize your top two technical accomplishments."
    record_attempt(
        session,
        job=listing,
        lookup_url=listing.url,
        mode="dry_run",
        result=ApplierResult(
            "manual_review",
            {"Email": "sample@example.com", accomplishments: None, "Pronouns": None},
            None,
            None,
            suggested_answers={accomplishments: "I built a <fast> Python service."},
            field_notes={"Pronouns": "No saved answer."},
        ),
        fit=FitSummary(
            score=91,
            reasons=["Strong Python match"],
            dealbreakers=[],
            recommended_action="apply",
        ),
        review_path=None,
    )
    session.flush()

    page = tmp_path / "dashboard.html"
    dashboard.write_dashboard(session, page, fit_threshold=70)
    html = page.read_text(encoding="utf-8")

    assert "Strong Python match" in html
    assert "recommended: apply" in html
    assert "<span class='match'>91</span>" in html
    assert escape(accomplishments) in html
    assert "I built a &lt;fast&gt; Python service." in html
    assert "Draft, not filled" in html
    assert "No saved answer." in html
    assert "1 of 3 answered" in html


def test_removed_jobs_are_hidden_and_server_saves_status_changes(tmp_path: Path) -> None:
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer

    from db.session import create_session_factory

    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as db:
        keep = add_job(db, "https://jobs.lever.co/a/keep", company="Keep Co")
        gone = add_job(db, "https://jobs.lever.co/a/gone", company="Gone Co")
        db.commit()
        keep_id, gone_id = keep.id, gone.id

    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(factory, 0))
    port = server.server_address[1]
    server.RequestHandlerClass = dashboard.make_handler(factory, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(job_id: int, status: str, headers: dict[str, str]) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST", f"/jobs/{job_id}/status", json.dumps({"status": status}), headers
        )
        return connection.getresponse().status

    try:
        trusted = {"X-Job-Dashboard": "1", "Content-Type": "application/json"}
        assert post(gone_id, "removed", {}) == 404
        assert post(gone_id, "removed", {**trusted, "Host": "evil.example"}) == 403
        assert post(gone_id, "interviewing", trusted) == 400
        assert post(gone_id, "removed", trusted) == 200
        assert post(keep_id, "applied", trusted) == 200

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/")
        page = connection.getresponse().read().decode()
    finally:
        server.shutdown()
        server.server_close()

    assert "Keep Co" in page and "Gone Co" not in page
    with factory() as db:
        assert db.get(Job, gone_id).status == "removed"
        assert db.get(Job, keep_id).status == "applied"
    engine.dispose()


def test_rows_offer_mark_applied_and_undo_back_to_where_they_were(
    session: Session, tmp_path: Path
) -> None:
    ready = add_job(session, "https://jobs.lever.co/a/ready", status="manual_review")
    session.add(Application(job_id=ready.id, mode="dry_run", answers={"Email": "x"}))
    done = add_job(session, "https://jobs.lever.co/a/done", status="applied")
    session.add(Application(job_id=done.id, mode="hand_off", answers={}))
    manual = add_job(session, "https://jobs.lever.co/a/manual", status="applied")
    session.flush()

    page = tmp_path / "dashboard.html"
    dashboard.write_dashboard(session, page, fit_threshold=70)
    html = page.read_text(encoding="utf-8")

    assert f"data-job='{ready.id}' data-status='applied'" in html
    assert f"data-job='{done.id}' data-status='manual_review'" in html
    assert f"data-job='{manual.id}' data-status='new'" in html
    assert "Mark applied" in html and "Applied ✓ · Undo" in html
    assert f"data-job='{ready.id}' data-restore='manual_review'" in html
    assert 'id="readonly"' in html


@pytest.mark.parametrize(
    ("title", "wanted"),
    [
        ("AI Engineer", True),
        ("Agent Engineer", True),
        ("AI Agent Engineer — Patient Intake", True),
        ("Forward Deployed Engineer, Agentic Platform", True),
        ("Machine Learning Engineer", True),
        ("Software Engineer, AI/ML Platform", True),
        ("LLM Research Scientist", True),
        ("Software Engineer, Infrastructure Security", False),
        ("Data Scientist, North Insights", False),
        ("Strategic Account Executive, AI", False),
        ("AI Transformation Owner, CRO", False),
        ("Email Engineer", False),
    ],
)
def test_title_scope_keeps_ai_and_agent_engineering_roles(title: str, wanted: bool) -> None:
    from agent.settings import title_in_scope

    assert title_in_scope(title, load_settings()) is wanted


def test_cleanup_removes_out_of_scope_jobs_but_never_applied_ones(session: Session) -> None:
    keep = add_job(session, "https://jobs.lever.co/a/keep", status="skipped", fit_score=40)
    intern = add_job(session, "https://jobs.lever.co/a/intern", title="AI Engineer Intern")
    sales = add_job(
        session, "https://jobs.lever.co/a/sales", title="Account Executive", status="skipped"
    )
    applied = add_job(
        session, "https://jobs.lever.co/a/applied", title="Java Developer", status="applied"
    )

    assert pipeline.remove_out_of_scope_jobs(session, load_settings()) == 2

    assert keep.status == "skipped"
    assert intern.status == "removed" and sales.status == "removed"
    assert applied.status == "applied"
    assert "Not an AI or agent engineering role" in sales.dealbreakers


def test_cleanup_removes_jobs_outside_nc_or_remote(session: Session) -> None:
    onsite = add_job(
        session,
        "https://jobs.lever.co/a/ca",
        status="queued",
        location_raw="Mountain View, California, United States",
        location_category="other",
    )
    unclear = add_job(
        session,
        "https://jobs.lever.co/a/us",
        status="queued",
        location_raw="United States",
        location_category="other",
    )
    raleigh = add_job(session, "https://jobs.lever.co/a/nc", location_raw="Raleigh, NC")

    assert pipeline.remove_out_of_scope_jobs(session, load_settings()) == 1

    assert onsite.status == "removed"
    assert unclear.status == "queued" and raleigh.status == "new"
