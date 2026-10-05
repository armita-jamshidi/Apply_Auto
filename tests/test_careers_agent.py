"""The careers agent: company titles taken only from pages it read, and fresh postings first."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine

from agent import dashboard
from agent.careers_agent import (
    CareersAgent,
    CompanyRole,
    Page,
    SiteReader,
    application_link,
    apply_role,
    check_company_pages,
    read_html,
)
from agent.filters import persist_job_if_new
from agent.sources.company_apply import BoardCache
from agent.types import JobListing
from db.models import Base, Job
from db.session import create_session_factory

CAREERS = """<html><head><title>Careers at Acme</title>
<script type="application/ld+json">{"@type": "JobPosting", "datePosted": "2026-10-01"}</script>
</head><body><nav><a href="/about">About</a></nav>
<h1>Open roles</h1>
<a href="/careers/applied-ai-engineer">Applied AI Engineer &ndash; Agents</a>
<a href="/careers/senior-ml">Senior ML Engineer</a>
<script>var hidden = "Staff AI Engineer";</script>
</body></html>"""
JOB = {
    "company": "Acme",
    "title": "AI Engineer (Remote)",
    "location": "Remote - US",
    "url": "https://himalayas.app/companies/acme/jobs/ai-engineer",
    "description": "Apply at https://acme.example/careers. Build agents.",
}


def test_read_html_keeps_visible_text_links_and_the_posting_date() -> None:
    page = read_html(CAREERS, "https://acme.example/careers")

    assert page.title == "Careers at Acme"
    assert "Applied AI Engineer – Agents" in page.text
    assert "Staff AI Engineer" not in page.text
    assert ("Applied AI Engineer – Agents", "https://acme.example/careers/applied-ai-engineer") in (
        page.links
    )
    assert page.posted_at == datetime(2026, 10, 1, tzinfo=UTC)


def _reader(routes: dict[str, httpx.Response]) -> SiteReader:
    def handle(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return SiteReader(httpx.Client(transport=httpx.MockTransport(handle)))


def test_reader_follows_robots_txt_and_stops_at_bot_checks() -> None:
    reader = _reader(
        {
            "https://shy.example/robots.txt": httpx.Response(
                200, text="User-agent: *\nDisallow: /"
            ),
            "https://guarded.example/jobs": httpx.Response(403, text="Access denied"),
            "https://open.example/jobs": httpx.Response(
                200, text=CAREERS, headers={"content-type": "text/html"}
            ),
        }
    )

    assert "robots.txt" in reader.read("https://shy.example/jobs").blocked
    assert "refused" in reader.read("https://guarded.example/jobs").blocked
    assert "already refused" in reader.read("https://guarded.example/other").blocked
    assert reader.read("https://open.example/jobs").title == "Careers at Acme"


def call(name: str, arguments: dict, number: int) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=arguments, id=f"call{number}")


class ScriptedModel:
    """Returns one prepared response per request and records the requests."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request):
        self.requests.append(request)
        return self.responses.pop(0)


def turn(*calls: SimpleNamespace, stop: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop, content=list(calls))


class FakeReader:
    def __init__(self, pages: dict[str, Page]) -> None:
        self.pages = pages

    def read(self, url: str) -> Page:
        return self.pages.get(url, Page(url, 404, blocked="HTTP 404."))


def _agent(model: ScriptedModel, boards: BoardCache | None = None) -> CareersAgent:
    page = read_html(CAREERS, "https://acme.example/careers")
    return CareersAgent(
        client=model,
        model="test-model",
        reader=FakeReader({"https://acme.example/careers": page}),
        boards=boards or BoardCache({}),
    )


def _report(url: str | None, title: str | None) -> SimpleNamespace:
    return call("report_result", {"url": url, "title": title, "reason": "Same role."}, 9)


def test_agent_takes_the_title_exactly_as_the_company_writes_it() -> None:
    model = ScriptedModel(
        [
            turn(stop="pause_turn"),  # a paused web search resumes the same turn
            turn(call("fetch_page", {"url": "https://acme.example/careers"}, 1)),
            turn(
                _report(
                    "https://acme.example/careers/applied-ai-engineer",
                    "Applied AI Engineer - Agents",
                )
            ),
        ]
    )

    outcome = _agent(model).find(JOB)

    assert outcome.role is not None
    # The page's own en dash is kept, not the model's hyphen.
    assert outcome.role.title == "Applied AI Engineer – Agents"
    assert outcome.role.url == "https://acme.example/careers/applied-ai-engineer"
    history = model.requests[-1]["messages"]
    result = next(
        json.loads(item["content"][0]["content"])
        for item in history
        if item["role"] == "user" and isinstance(item["content"], list)
    )
    assert result["links"][0]["text"] != "About"  # careers links are listed first
    assert any(tool.get("name") == "web_search" for tool in model.requests[0]["tools"])


@pytest.mark.parametrize(
    ("url", "title", "why"),
    [
        ("https://acme.example/careers/applied-ai-engineer", "AI Engineer", "was not found"),
        ("https://acme.example/careers/senior-ml", "Senior ML Engineer", "more senior"),
        ("https://acme.example/careers/staff", "Staff AI Engineer", "more senior"),
        ("https://www.linkedin.com/jobs/view/1", "AI Engineer", "job board"),
        ("https://acme.example/careers/never-read", "Platform Engineer", "was not found"),
    ],
)
def test_reports_not_backed_by_a_page_it_read_are_rejected(url, title, why) -> None:
    model = ScriptedModel(
        [
            turn(call("fetch_page", {"url": "https://acme.example/careers"}, 1)),
            turn(_report(url, title)),
        ]
    )

    outcome = _agent(model).find(JOB)

    assert outcome.role is None and why in outcome.reason


def test_board_listings_supply_the_title_and_date() -> None:
    posted = datetime(2026, 10, 2, tzinfo=UTC)
    listing = JobListing(
        "greenhouse",
        "greenhouse",
        "Acme",
        "AI Engineer, Agents",
        "https://job-boards.greenhouse.io/acme/jobs/123",
        "Remote",
        "Build agents.",
        posted_at=posted,
    )
    boards = BoardCache({"greenhouse": lambda *_args, **_kwargs: [listing]})
    model = ScriptedModel(
        [
            turn(call("list_board", {"platform": "greenhouse", "board": "acme"}, 1)),
            turn(_report(listing.url, "AI Engineer, Agents")),
        ]
    )

    outcome = _agent(model, boards).find(JOB)

    assert outcome.role.listing == listing and outcome.role.posted_at == posted


@pytest.fixture
def session(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        yield session
    engine.dispose()


def _job(session, **overrides) -> Job:
    values = {
        "source": "himalayas",
        "platform": "himalayas",
        "company": "Acme",
        "title": "AI Engineer (Remote)",
        "url": JOB["url"],
        "location_raw": "Remote - US",
        "location_category": "remote_us",
        "description": JOB["description"],
        "status": "new",
    }
    job = Job(**(values | overrides))
    session.add(job)
    session.commit()
    return job


def test_apply_role_retitles_and_rediscovery_does_not_duplicate(session) -> None:
    job = _job(session)
    posted = datetime(2026, 10, 1, tzinfo=UTC)
    role = CompanyRole(
        "https://acme.example/careers/applied-ai-engineer",
        "Applied AI Engineer",
        "Same role.",
        posted_at=posted,
    )

    assert apply_role(session, job, role)
    session.commit()

    assert job.title == "Applied AI Engineer" and job.listed_title == "AI Engineer (Remote)"
    assert job.apply_url == role.url and job.platform == "himalayas"
    assert job.posted_at.replace(tzinfo=UTC) == posted
    again = JobListing(
        "himalayas", "himalayas", "Acme", "AI Engineer (Remote)", "https://other.example/1", "", ""
    )
    assert persist_job_if_new(session, again, "remote_us") is False


def test_a_board_job_found_on_the_company_site_becomes_fillable(session) -> None:
    job = _job(session)
    role = CompanyRole(
        "https://job-boards.greenhouse.io/acme/jobs/123", "AI Engineer", "Same role."
    )

    apply_role(session, job, role)

    assert job.platform == "greenhouse"
    assert job.url == "https://job-boards.greenhouse.io/acme/jobs/123"


def test_each_job_is_checked_once(session, monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(session)
    seen: list[str] = []

    def fake_find(detail, **_kwargs):
        seen.append(detail["title"])
        from agent.careers_agent import Outcome

        return Outcome(None, "Not listed.")

    monkeypatch.setattr("agent.careers_agent.find_company_role", fake_find)

    check_company_pages(session, model="m", client=object())
    check_company_pages(session, model="m", client=object())

    assert seen == ["AI Engineer (Remote)"]
    assert job.company_page_checked_at is not None and job.title == "AI Engineer (Remote)"


def test_dashboard_lists_fresh_postings_first_and_shows_the_listed_title(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dashboard, "_quick_answers", lambda: [])
    now = datetime.now(UTC)
    _job(
        session,
        title="Old Match",
        url="https://a.example/1",
        fit_score=95,
        posted_at=now - timedelta(days=30),
    )
    _job(
        session,
        title="Applied AI Engineer",
        listed_title="AI Engineer (Remote)",
        url="https://a.example/2",
        fit_score=70,
        posted_at=now - timedelta(days=2),
    )

    rows = dashboard.write_dashboard(
        session, tmp_path / "dashboard.html", fit_threshold=70, fresh_days=7
    )

    assert [row.job.title for row in rows] == ["Applied AI Engineer", "Old Match"]
    html = (tmp_path / "dashboard.html").read_text(encoding="utf-8")
    assert "Listed elsewhere as AI Engineer (Remote)" in html
    assert html.count("class='pill fresh'") == 1
    assert 'data-filter="fresh"' in html and "data-fresh='1'" in html
    assert "Posted in the last 7 days" in html


def test_a_postings_apply_button_leads_to_its_application_form() -> None:
    page = Page(
        "https://acme.example/careers/ai-engineer",
        200,
        links=[
            ("Careers", "https://acme.example/careers"),
            ("Apply now", "https://acme.example/careers/ai-engineer/apply"),
            ("Apply", "https://acme.zohorecruit.com/jobs/Careers/1/AI-Engineer"),
        ],
    )

    assert application_link(page) == "https://acme.zohorecruit.com/jobs/Careers/1/AI-Engineer"
    page.links = page.links[:2]
    assert application_link(page) == "https://acme.example/careers/ai-engineer/apply"
    page.links = page.links[:1]
    assert application_link(page) is None
