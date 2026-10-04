"""Tests for posting dates, the posting-age cutoff, title matching, and duplicate roles."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import dashboard, pipeline
from agent.fetchers.http import parse_posted
from agent.filters import persist_job_if_new
from agent.settings import load_settings, stale_posting_reason
from agent.sources.company_apply import titles_match
from agent.types import JobListing
from db.models import Base, Job


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-02T11:31:50-04:00",
        "2026-10-02T15:31:50.000Z",
        "Fri, 02 Oct 2026 15:31:50 +0000",
        1790955110,
        1790955110000,
        "1790955110",
    ],
)
def test_parse_posted_reads_every_source_format(value: object) -> None:
    posted = parse_posted(value)

    assert posted is not None and posted.tzinfo is not None
    assert posted.astimezone(UTC).date().isoformat() == "2026-10-02"


@pytest.mark.parametrize("value", [None, "", "not a date"])
def test_parse_posted_ignores_missing_or_bad_dates(value: object) -> None:
    assert parse_posted(value) is None


def test_postings_older_than_sixty_days_are_stale() -> None:
    settings = load_settings()
    now = datetime.now(UTC)

    assert settings.max_posting_age_days == 60
    assert stale_posting_reason(now - timedelta(days=61), settings).startswith("Posted 61 days")
    assert stale_posting_reason(now - timedelta(days=59), settings) is None
    assert stale_posting_reason(None, settings) is None


@pytest.mark.parametrize(
    ("posted", "listed", "same"),
    [
        ("AI Harness Engineer", "AI Harness Engineer (100% Remote, Worldwide)", True),
        ("AI Applied Engineer", "Applied AI Engineer", True),
        ("Machine Learning Engineer II/III (Research)", "Machine Learning Engineer III", True),
        ("ML Engineer - Remote", "ML Engineer", True),
        ("Machine Learning Engineer | Upto $85/hr", "Machine Learning Engineer", True),
        ("Agent Engineer", "Senior Agent Engineer", False),
        ("Machine Learning Engineer", "Machine Learning Engineer, Frontier Data Products", False),
        ("AI Engineer", "AI Engineer II", False),
        ("Software Engineer", "Software Engineering", False),
    ],
)
def test_titles_match(posted: str, listed: str, same: bool) -> None:
    assert titles_match(posted, listed) is same


def listing(url: str, company: str, title: str, posted: datetime | None = None) -> JobListing:
    return JobListing(
        "himalayas", "himalayas", company, title, url, "Remote - US", "Build agents.",
        posted_at=posted,
    )


def test_same_role_at_same_company_is_not_stored_twice(session: Session) -> None:
    posted = datetime(2026, 9, 28, tzinfo=UTC)
    assert persist_job_if_new(
        session, listing("https://himalayas.app/a", "Direct Supply, Inc.", "AI Engineer"),
        "remote_us",
    )
    stored = session.query(Job).one()
    # Later the stored job's link is changed to the company's own board...
    stored.url = "https://jobs.lever.co/directsupply/1"
    session.flush()

    # ...so the same posting comes back from the board with its original link.
    again = listing("https://himalayas.app/a", "Direct Supply", "AI  engineer", posted)
    assert not persist_job_if_new(session, again, "remote_us")
    assert session.query(Job).count() == 1
    assert stored.posted_at.replace(tzinfo=UTC) == posted

    other = listing("https://himalayas.app/b", "Direct Supply", "Agent Engineer")
    assert persist_job_if_new(session, other, "remote_us")


def test_cleanup_removes_stale_jobs_and_dashboard_shows_posted(session: Session, tmp_path) -> None:
    now = datetime.now(UTC)
    old = Job(
        source="lever", platform="lever", company="Old Co", title="AI Engineer",
        url="https://jobs.lever.co/old/1", location_raw="Remote - US",
        location_category="remote_us", description="d", status="new",
        posted_at=now - timedelta(days=90),
    )
    fresh = Job(
        source="lever", platform="lever", company="Fresh Co", title="AI Engineer",
        url="https://jobs.lever.co/fresh/1", location_raw="Remote - US",
        location_category="remote_us", description="d", status="new",
        posted_at=now - timedelta(days=3),
    )
    session.add_all([old, fresh])
    session.flush()

    assert pipeline.remove_out_of_scope_jobs(session, load_settings()) == 1
    assert old.status == "removed" and fresh.status == "new"
    assert old.dealbreakers[0].startswith("Posted 90 days ago")

    page = tmp_path / "dashboard.html"
    dashboard.write_dashboard(session, page, fit_threshold=70)
    html = page.read_text(encoding="utf-8")
    assert "<th>Posted</th>" in html
    assert "3 days ago" in html


def test_company_hosted_greenhouse_jobs_keep_the_company_page_to_apply() -> None:
    import httpx

    from agent.fetchers.greenhouse import fetch_greenhouse_jobs

    payload = {
        "jobs": [
            {"id": 7, "title": "AI Engineer", "absolute_url": "https://acme.com/careers?gh_jid=7"},
            {
                "id": 8,
                "title": "ML Engineer",
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/8",
            },
        ]
    }
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    client = httpx.Client(transport=transport)

    hosted, plain = fetch_greenhouse_jobs("acme", "Acme", client=client)

    assert hosted.url == "https://job-boards.greenhouse.io/acme/jobs/7"
    assert hosted.apply_url == "https://acme.com/careers?gh_jid=7"
    assert (plain.url, plain.apply_url) == ("https://job-boards.greenhouse.io/acme/jobs/8", None)


def test_rediscovery_repairs_a_stored_company_page_link(session: Session, tmp_path) -> None:
    old = Job(
        source="greenhouse", platform="greenhouse", company="Acme", title="AI Engineer",
        url="https://acme.com/careers?gh_jid=7", location_raw="Remote - US",
        location_category="remote_us", description="d", status="new", fit_score=88,
    )
    session.add(old)
    session.flush()
    fresh = JobListing(
        "greenhouse", "greenhouse", "Acme", "AI Engineer",
        "https://job-boards.greenhouse.io/acme/jobs/7", "Remote - US", "d",
        apply_url="https://acme.com/careers?gh_jid=7",
    )

    assert not persist_job_if_new(session, fresh, "remote_us")

    assert old.url == "https://job-boards.greenhouse.io/acme/jobs/7"
    assert old.apply_url == "https://acme.com/careers?gh_jid=7"
    assert pipeline.jobs_to_prepare(session, load_settings(), limit=5) == [old]
    page = tmp_path / "dashboard.html"
    dashboard.write_dashboard(session, page, fit_threshold=70)
    html = page.read_text(encoding="utf-8")
    assert "href='https://acme.com/careers?gh_jid=7' class='apply-link'" in html
    assert "--job-url &quot;https://job-boards.greenhouse.io/acme/jobs/7&quot;" in html


def test_jobs_off_their_platform_host_are_not_prepared(session: Session) -> None:
    job = Job(
        source="greenhouse", platform="greenhouse", company="Acme", title="AI Engineer",
        url="https://acme.com/careers?gh_jid=9", location_raw="Remote - US",
        location_category="remote_us", description="d", status="new", fit_score=95,
    )
    session.add(job)
    session.flush()

    assert pipeline.jobs_to_prepare(session, load_settings(), limit=5) == []


def test_same_role_handles_wildcard_titles_and_company_suffixes(session: Session) -> None:
    assert persist_job_if_new(session, listing("https://x/1", "Acme", "%AI Engineer"), "remote_us")
    assert persist_job_if_new(session, listing("https://x/2", "Acme", "_ML Engineer"), "remote_us")
    assert not persist_job_if_new(
        session, listing("https://x/3", "Acme, Inc.", "%AI Engineer"), "remote_us"
    )
    assert session.query(Job).count() == 2


def test_cap_treats_company_suffixes_as_the_same_company(session: Session) -> None:
    for index, company in enumerate(["Direct Supply, Inc.", "Direct Supply", "direct supply"]):
        session.add(
            Job(
                source="lever", platform="lever", company=company, title=f"AI Engineer {index}",
                url=f"https://jobs.lever.co/ds/{index}", location_raw="Remote - US",
                location_category="remote_us", description="d", status="new",
                fit_score=90 - index,
            )
        )
    session.flush()

    assert pipeline.cap_jobs_per_company(session, load_settings()) == 1
