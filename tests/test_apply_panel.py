"""The dashboard's Apply panel: prepared answers, asked questions, and the candidate's details."""

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, select

from agent import dashboard
from agent.applier.greenhouse import ApplierResult
from agent.applier.review import FitSummary, profile_quick_answers, write_review_page
from agent.types import JobListing
from db.models import Application, Base, Job
from db.session import create_session_factory

PROFILE = {
    "personal": {"name": "Sam Q Sample", "email": "sam@example.com", "github": "https://gh/sam"},
    "work_authorization": {"authorized_to_work_in_us": True, "requires_sponsorship": False},
    "education": [{"institution": "Example U", "degree": "BS CS"}],
    "application_answers": {
        "willing_to_relocate": False,
        "languages": ["English", "Spanish"],
        "eeo": {"gender": "decline"},
    },
}


def test_quick_answers_label_every_common_field() -> None:
    answers = dict(profile_quick_answers(PROFILE))

    assert answers["First name"] == "Sam" and answers["Last name"] == "Q Sample"
    assert answers["Requires visa sponsorship now or in the future"] == "No"
    assert answers["Languages"] == "English, Spanish"
    assert answers["Willing to relocate"] == "No"
    assert answers["School"] == "Example U"
    assert answers["Voluntary self-identification: gender"] == "Decline to self-identify"


def test_review_page_lists_details_even_when_the_form_was_blocked(tmp_path: Path) -> None:
    page = tmp_path / "review.html"
    blocked = ApplierResult("manual_review", {}, None, None, "Behind a bot check.")

    write_review_page(
        page,
        job=JobListing("x", "x", "Acme", "AI Engineer", "https://acme.example", "", ""),
        result=blocked,
        fit=FitSummary(),
        resume_name="resume.pdf",
        quick_answers=profile_quick_answers(PROFILE),
    )

    html = page.read_text(encoding="utf-8")
    assert "<h2>Your details</h2>" in html
    assert "data-copy='sam@example.com'" in html


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dashboard, "_quick_answers", lambda: profile_quick_answers(PROFILE))
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        job = Job(
            source="smartrecruiters",
            platform="smartrecruiters",
            company="Acme",
            title="ML Engineer",
            url="https://jobs.smartrecruiters.com/acme/1",
            location_raw="Remote - US",
            location_category="remote_us",
            description="Build ML.",
            status="manual_review",
        )
        session.add(job)
        session.flush()
        session.add(
            Application(
                job_id=job.id,
                mode="dry_run",
                answers={"Email": "sam@example.com", "Why us?": None},
                suggested_answers={"Why us?": "I build ML systems."},
            )
        )
        session.commit()
        job_id = job.id
    asked: list[str] = []

    def answer(job: Job, question: str) -> dict:
        asked.append(f"{job.company}: {question}")
        return {"kind": "draft", "answer": "I shipped an agent.", "note": "Review it."}

    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(factory, 0))
    port = server.server_address[1]
    server.RequestHandlerClass = dashboard.make_handler(factory, port, answer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/", factory, job_id, asked
    server.shutdown()
    server.server_close()
    engine.dispose()


def test_apply_panel_shows_answers_and_asks_new_questions(served) -> None:
    url, factory, job_id, asked = served
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(chromium_sandbox=True)
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)

        page.click("button.panel-open")
        panel = page.locator("#panel")
        assert panel.is_visible()
        assert page.text_content("#panel-title") == "ML Engineer"
        assert "I build ML systems." in page.text_content("#panel-answers")
        assert (
            page.get_attribute("#panel-apply", "href") == "https://jobs.smartrecruiters.com/acme/1"
        )
        assert "sam@example.com" in panel.text_content()

        page.fill("#panel-question", "What is the most impactful thing you've built?")
        page.click("#panel-ask")
        page.wait_for_selector("#panel-asked li")
        assert "I shipped an agent." in page.text_content("#panel-asked")
        assert "draft: review it" in page.text_content("#panel-asked")

        page.click("#panel-close")
        assert not panel.is_visible()
        assert errors == []
        browser.close()

    assert asked == ["Acme: What is the most impactful thing you've built?"]
    with factory() as session:
        attempt = session.scalar(select(Application).where(Application.job_id == job_id))
        question = "What is the most impactful thing you've built?"
        assert attempt.suggested_answers[question] == "I shipped an agent."
        assert attempt.answers[question] is None
        assert attempt.answers["Email"] == "sam@example.com"


def test_fill_with_agent_starts_the_agent_on_the_company_form(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "_quick_answers", lambda: [])
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    common = {
        "source": "himalayas",
        "platform": "himalayas",
        "company": "Acme",
        "location_raw": "Remote - US",
        "location_category": "remote_us",
        "status": "new",
    }
    with factory() as session:
        session.add_all(
            [
                Job(
                    title="AI Solutions Engineer",
                    url="https://himalayas.app/companies/acme/jobs/1",
                    apply_url="https://careers-acme.icims.com/jobs/1/job",
                    **common,
                ),
                Job(
                    title="ML Engineer",
                    url="https://himalayas.app/companies/acme/jobs/2",
                    apply_url="https://himalayas.app/companies/acme/jobs/2",
                    **common,
                ),
            ]
        )
        session.commit()
    started: list[list[str]] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(factory, 0))
    port = server.server_address[1]
    problems: list[str | None] = [None]
    server.RequestHandlerClass = dashboard.make_handler(
        factory, port, launch=started.append, check_api=lambda: problems[0]
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(chromium_sandbox=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/")
            assert page.locator("#jobs button.fill").count() == 1  # the board-only job has none
            page.click("tr[data-title='AI Solutions Engineer'] button.fill")
            page.wait_for_selector("#toast:not([hidden])")
            assert "new window" in page.text_content("#toast")
            page.click("tr[data-title='AI Solutions Engineer'] button.panel-open")
            assert page.is_visible("#panel-fill")
            page.click("tr[data-title='ML Engineer'] button.panel-open")
            assert not page.is_visible("#panel-fill")
            # Without API credits nothing is opened, and the dashboard says why.
            problems[0] = "Your Anthropic API credits have run out."
            page.click("#panel-close")
            page.click("tr[data-title='AI Solutions Engineer'] button.fill")
            page.wait_for_function(
                "document.getElementById('toast').textContent.includes('credits')"
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        engine.dispose()

    assert started == [
        [
            started[0][0],
            "-m",
            "agent.form_agent",
            "--job-url",
            "https://careers-acme.icims.com/jobs/1/job",
            "--lookup-url",
            "https://himalayas.app/companies/acme/jobs/1",
        ]
    ]
