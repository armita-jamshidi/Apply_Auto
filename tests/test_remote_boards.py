"""Tests for the We Work Remotely and Hacker News job sources, and the per-company cap."""

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import pipeline
from agent.filters import normalize_location
from agent.settings import load_settings, title_in_scope
from agent.sources.remote_boards import (
    fetch_hn_whos_hiring,
    fetch_weworkremotely,
    remote_us_label,
)
from db.models import Base, Job

WWR_FEED = b"""<?xml version="1.0"?><rss><channel>
<item><title>Acme: AI Engineer</title><region>USA Only</region>
<link>https://weworkremotely.com/remote-jobs/acme-ai-engineer</link>
<description>Build agents.</description></item>
<item><title>Euro Co: ML Engineer</title><region>Europe Only</region>
<link>https://weworkremotely.com/remote-jobs/euro</link><description>x</description></item>
<item><title>World Co: Agent Engineer</title><region>Anywhere in the World</region>
<link>https://weworkremotely.com/remote-jobs/world</link><description>y</description></item>
</channel></rss>"""


def client_for(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_wwr_keeps_jobs_open_to_the_us() -> None:
    client = client_for({"https://weworkremotely.com": httpx.Response(200, content=WWR_FEED)})

    jobs = fetch_weworkremotely(client)

    assert [(job.company, job.title) for job in jobs] == [
        ("Acme", "AI Engineer"),
        ("World Co", "Agent Engineer"),
    ]
    assert all(normalize_location(job.location_raw) == "remote_us" for job in jobs)
    assert jobs[0].platform == "weworkremotely"


@pytest.mark.parametrize(
    ("region", "label"),
    [
        ("USA Only", "Remote - US (USA Only)"),
        ("North America Only", "Remote - US (North America Only)"),
        ("Europe Only", None),
        ("", "Remote - US"),
    ],
)
def test_remote_us_label(region: str, label: str | None) -> None:
    assert remote_us_label(region) == label


def test_hn_thread_parses_role_and_location_from_the_first_line() -> None:
    settings = load_settings()
    thread = {
        "children": [
            {"id": 1, "text": "Acme | AI Engineer | REMOTE (US) | $150k<p>We build agents."},
            {"id": 2, "text": "Shop | Barista | Raleigh, NC<p>Coffee."},
            {"id": 3, "text": "Euro AI | ML Engineer | REMOTE (CET +-2)<p>Models."},
            {"id": 4, "text": "Anywhere AI | Agent Engineer | Remote<p>Agents."},
            {"id": 5, "text": "No pipes here"},
        ]
    }
    client = client_for(
        {
            "https://hn.algolia.com/api/v1/search_by_date": httpx.Response(
                200, json={"hits": [{"title": "Ask HN: Who is hiring? (October)", "objectID": 9}]}
            ),
            "https://hn.algolia.com/api/v1/items/9": httpx.Response(200, json=thread),
        }
    )

    jobs = fetch_hn_whos_hiring(client, title_filter=lambda t: title_in_scope(t, settings))

    by_company = {job.company: job for job in jobs}
    assert set(by_company) == {"Acme", "Euro AI", "Anywhere AI"}
    assert by_company["Acme"].url == "https://news.ycombinator.com/item?id=1"
    assert normalize_location(by_company["Acme"].location_raw) == "remote_us"
    assert normalize_location(by_company["Anywhere AI"].location_raw) == "remote_us"
    assert normalize_location(by_company["Euro AI"].location_raw) == "other"
    assert "We build agents." in by_company["Acme"].description


def test_cap_keeps_the_best_two_jobs_per_company() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = load_settings()
    with Session(engine) as session:
        def add(title: str, fit: int | None, status: str = "new", company: str = "Acme") -> Job:
            job = Job(
                source="lever", platform="lever", company=company, title=title,
                url=f"https://jobs.lever.co/acme/{title}", location_raw="Remote - US",
                location_category="remote_us", description="d", fit_score=fit, status=status,
            )
            session.add(job)
            session.flush()
            return job

        ready = add("ready", 50, status="manual_review")
        best = add("best", 90)
        low = add("low", 60)
        unscored = add("unscored", None)
        applied = add("applied", 99, status="applied")
        other = add("other", 10, company="Other Co")

        assert settings.max_jobs_per_company == 2
        assert pipeline.cap_jobs_per_company(session, settings) == 2

        assert (ready.status, best.status) == ("manual_review", "new")
        assert low.status == unscored.status == "removed"
        assert applied.status == "applied" and other.status == "new"
        assert "Kept the 2 most relevant roles at Acme" in low.dealbreakers
    engine.dispose()


@pytest.mark.parametrize(
    ("title", "description", "allowed"),
    [
        ("Machine Learning Engineer II", "Requires 2+ years of experience with Python.", True),
        ("Machine Learning Engineer II", "Requires 3+ years of experience with Python.", False),
        ("Machine Learning Engineer II", "Build models.", False),
        ("AI Engineer", "Requires 4 years of experience.", False),
        ("AI Engineer", "Requires 1+ years of experience.", True),
        ("AI Engineer", "Build agents.", True),
    ],
)
def test_roles_needing_more_than_two_years_are_excluded(
    title: str, description: str, allowed: bool
) -> None:
    from agent.settings import excluded_role_reason

    settings = load_settings()
    assert "mid" in settings.experience_levels
    assert (excluded_role_reason(title, description, settings) is None) is allowed
