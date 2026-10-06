"""Closed postings are hidden, and job board links are named for what they are."""

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import dashboard
from agent.postings import CLOSED, close_finished_postings
from agent.sources.company_apply import BoardCache
from agent.types import JobListing
from db.models import Base, Job


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def _job(session: Session, url: str, platform: str, **overrides) -> Job:
    values = dict(
        source=platform, platform=platform, company="Acme", title="AI Engineer", url=url,
        location_raw="Remote - US", location_category="remote_us", description="d",
        status="new",
    )
    values.update(overrides)
    job = Job(**values)
    session.add(job)
    session.flush()
    return job


ROUTES = {
    "https://jobs.lever.co/acme/gone": httpx.Response(404),
    "https://jobs.lever.co/acme/open": httpx.Response(200, text="AI Engineer"),
    "https://job-boards.greenhouse.io/acme/jobs/1": httpx.Response(
        302, headers={"location": "https://job-boards.greenhouse.io/acme?error=true"}
    ),
    "https://job-boards.greenhouse.io/acme?error=true": httpx.Response(200, text="Jobs"),
    "https://acme.wd1.myworkdayjobs.com/ext/job/x": httpx.Response(403, text="denied"),
}


def test_closed_postings_are_hidden_and_unclear_ones_are_kept(session: Session) -> None:
    gone = _job(session, "https://jobs.lever.co/acme/gone", "lever")
    open_ = _job(session, "https://jobs.lever.co/acme/open", "lever", title="ML Engineer")
    redirected = _job(session, "https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse")
    blocked = _job(session, "https://acme.wd1.myworkdayjobs.com/ext/job/x", "workday")
    ashby_gone = _job(session, "https://jobs.ashbyhq.com/acme/old-id", "ashby")
    ashby_open = _job(session, "https://jobs.ashbyhq.com/acme/live-id", "ashby", title="LLM")
    boards = BoardCache(
        {
            "ashby": lambda *_args, **_kwargs: [
                JobListing("ashby", "ashby", "Acme", "LLM", "https://jobs.ashbyhq.com/acme/live-id",
                           "", "")
            ]
        }
    )
    def route(request: httpx.Request) -> httpx.Response:
        return ROUTES.get(str(request.url), httpx.Response(404))

    client = httpx.Client(transport=httpx.MockTransport(route), follow_redirects=True)

    closed = close_finished_postings(session, boards=boards, client=client)

    assert closed == 3
    assert {gone.status, redirected.status, ashby_gone.status} == {CLOSED}
    assert open_.status == blocked.status == ashby_open.status == "new"
    assert gone not in [row.job for row in dashboard.dashboard_rows(session)]


def test_a_job_board_link_is_named_and_a_company_search_is_offered(
    session: Session, tmp_path
) -> None:
    _job(
        session, "https://himalayas.app/companies/acme/jobs/ai-engineer", "himalayas",
        apply_url="https://himalayas.app/companies/acme/jobs/ai-engineer",
    )
    page = tmp_path / "dashboard.html"

    dashboard.write_dashboard(session, page, fit_threshold=70)

    html = page.read_text(encoding="utf-8")
    assert "class='apply-link' target='_blank' rel='noopener'>Himalayas listing</a>" in html
    assert "Find on company site" in html and "google.com/search?q=" in html
