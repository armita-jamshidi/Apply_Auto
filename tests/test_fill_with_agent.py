"""Tests for Fill with agent: a busy browser profile, and a window that never sits blank."""

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent import dashboard, form_agent
from agent.applier import browser_profile, cli
from agent.types import JobListing
from db.models import Base, Job


def test_an_unused_profile_is_free(tmp_path: Path) -> None:
    assert not browser_profile.browser_profile_in_use(tmp_path / "missing")
    assert not browser_profile.browser_profile_in_use(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="SingletonLock is the macOS and Linux lock")
def test_singleton_lock_of_a_running_browser_means_busy(tmp_path: Path) -> None:
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    assert browser_profile.browser_profile_in_use(tmp_path)

    lock.unlink()
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    lock.symlink_to(f"{socket.gethostname()}-{ended.pid}")
    assert not browser_profile.browser_profile_in_use(tmp_path)  # left by a crash

    lock.unlink()
    lock.symlink_to(f"another-computer-{os.getpid()}")
    assert not browser_profile.browser_profile_in_use(tmp_path)


def test_windows_lockfile_held_open_means_busy(tmp_path: Path, monkeypatch) -> None:
    lock = tmp_path / "lockfile"
    lock.write_text("", encoding="utf-8")
    assert not browser_profile.browser_profile_in_use(tmp_path)  # left behind, not held

    real_open = Path.open

    def held_open(self: Path, *args, **kwargs):
        if self == lock:
            raise PermissionError("The process cannot access the file")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", held_open)
    assert browser_profile.browser_profile_in_use(tmp_path)


def test_a_real_browser_holds_its_profile(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    profile = tmp_path / "profile"
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(str(profile), headless=True)
        except PlaywrightError as error:
            pytest.skip(f"Chromium is not installed: {str(error).splitlines()[0]}")
        assert browser_profile.browser_profile_in_use(profile)
        context.close()
    assert not browser_profile.browser_profile_in_use(profile)


def test_hand_off_refuses_to_open_a_second_browser(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "browser_profile_in_use", lambda _profile: True)

    class NoBrowser:
        def __getattr__(self, name):
            raise AssertionError("no browser may be launched")

    with pytest.raises(SystemExit, match="still open"):
        cli.launch_hand_off_browser(NoBrowser(), "chrome", tmp_path / "profile")


def test_dashboard_says_the_browser_is_open_instead_of_launching(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add(
            Job(
                source="example",
                platform="example",
                company="Example Labs",
                title="AI Engineer",
                url="https://careers.example.com/jobs/1",
                apply_url="https://careers.example.com/jobs/1/apply",
                location_raw="Remote - US",
                location_category="remote_us",
                status="new",
            )
        )
        session.commit()
        job_id = session.query(Job.id).scalar()
    started: list[list[str]] = []
    busy = [True]
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(factory, 0))
    port = server.server_address[1]
    server.RequestHandlerClass = dashboard.make_handler(
        factory,
        port,
        launch=started.append,
        check_api=lambda: None,
        browser_busy=lambda: busy[0],
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def fill() -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/jobs/{job_id}/fill",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Job-Dashboard": "1",
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:  # noqa: S310 - local server
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        status, reply = fill()
        assert status == 409 and "still open" in reply["error"]
        assert started == []
        busy[0] = False
        status, _reply = fill()
        assert status == 200 and len(started) == 1
    finally:
        server.shutdown()
        server.server_close()
        engine.dispose()


class FakePage:
    def __init__(self, url: str = "about:blank", goto_error: str | None = None) -> None:
        self.url = url
        self.goto_error = goto_error
        self.content = ""
        self.in_front = False

    def bring_to_front(self) -> None:
        self.in_front = True

    def set_content(self, content: str) -> None:
        self.content = content

    def goto(self, url: str, **_kwargs) -> None:
        if self.goto_error:
            raise PlaywrightError(self.goto_error)
        self.url = url


class FakeBrowser:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


def test_agent_works_in_the_blank_tab_or_a_new_one_in_front() -> None:
    restored = FakePage("https://example.com/earlier-tab")
    blank = FakePage()
    assert form_agent.working_page(FakeBrowser([restored, blank])) is blank
    assert blank.in_front

    browser = FakeBrowser([restored])
    page = form_agent.working_page(browser)
    assert page is not restored and page.in_front and len(browser.pages) == 2


def test_a_page_that_will_not_open_is_explained_not_left_blank(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agent.form_agent.load_profile", lambda _path: {})
    monkeypatch.setattr("agent.form_agent.extract_resume_text", lambda _path: "Python.")
    page = FakePage(goto_error="net::ERR_CONNECTION_RESET at https://jobs.example.com/1\nlog")

    result = form_agent.run_form_agent(
        page,
        JobListing("x", "x", "Example Labs", "AI Engineer", "https://jobs.example.com/1", "", ""),
        tmp_path / "profile.yaml",
        tmp_path / "resume.pdf",
        client=object(),
        model="test-model",
    )

    assert result.error.startswith("Could not open the application page")
    assert result.answers == {}
    assert "did not open" in page.content and "https://jobs.example.com/1" in page.content


def test_errors_wait_for_enter_so_the_window_does_not_vanish(monkeypatch) -> None:
    def crash(_argv):
        raise RuntimeError("profile.yaml is missing")

    paused: list[str] = []
    monkeypatch.setattr(form_agent, "_main", crash)
    monkeypatch.setattr(form_agent, "_pause", paused.append)

    assert form_agent.main(["--job-url", "https://jobs.example.com/1"]) == 1
    assert paused and "Press Enter" in paused[0]

    def busy(_argv):
        raise SystemExit(browser_profile.PROFILE_IN_USE_MESSAGE)

    monkeypatch.setattr(form_agent, "_main", busy)
    with pytest.raises(SystemExit):
        form_agent.main(["--job-url", "https://jobs.example.com/1"])
    assert len(paused) == 2

    paused.clear()
    monkeypatch.setattr(form_agent, "_main", crash)
    assert form_agent.main(["--headless", "--job-url", "https://jobs.example.com/1"]) == 1
    assert paused == []  # nobody is at a headless run's console
