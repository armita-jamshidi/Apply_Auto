"""Tests for the morning run: which jobs get kits, the start time, and the schedulers."""

import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import daily
from agent.apply_kit import Kit, record_kit
from agent.settings import load_settings
from db.models import Base, Job

EASTERN = ZoneInfo("America/New_York")


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def add_job(db: Session, url: str, **fields) -> Job:
    values = {
        "source": "greenhouse",
        "platform": "greenhouse",
        "company": "Example Labs",
        "title": "AI Engineer",
        "url": url,
        "location_raw": "Remote - US",
        "location_category": "remote_us",
        "description": "Build agents in Python.",
        "status": "new",
        "fit_score": 80,
        **fields,
    }
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


def test_good_matches_without_a_kit_get_one_fresh_first(session: Session) -> None:
    now = datetime.now(UTC)
    older = add_job(session, "https://example.com/1", fit_score=95, posted_at=now - timedelta(30))
    fresh = add_job(session, "https://example.com/2", fit_score=75, posted_at=now)
    ready = add_job(session, "https://example.com/3", status="manual_review", fit_score=85)
    has_kit = add_job(session, "https://example.com/4", fit_score=99)
    record_kit(session, has_kit, Kit())
    add_job(session, "https://example.com/5", fit_score=40)
    add_job(session, "https://example.com/6", status="removed")
    add_job(session, "https://example.com/7", status="applied")
    add_job(session, "https://example.com/8", location_category="other")
    add_job(session, "https://example.com/9", dealbreakers=["Needs 8+ years"])
    add_job(session, "https://example.com/10", fit_recommendation="skip")
    add_job(session, "https://example.com/11", title="Account Executive")
    session.commit()

    chosen = daily.jobs_needing_kits(session, load_settings(), limit=10)

    assert [job.id for job in chosen] == [fresh.id, older.id, ready.id]
    assert daily.jobs_needing_kits(session, load_settings(), limit=1) == [fresh]
    assert daily.jobs_needing_kits(session, load_settings(), limit=0) == []


def test_one_failed_kit_does_not_stop_the_rest(session: Session) -> None:
    first = add_job(session, "https://example.com/1")
    second = add_job(session, "https://example.com/2")
    third = add_job(session, "https://example.com/3")
    built: list[int] = []

    def builder(_session: Session, job: Job) -> Kit:
        built.append(job.id)
        if job is first:
            raise RuntimeError("API unavailable")
        return Kit(problems=["Resume: no evidence"]) if job is second else Kit()

    assert daily.build_kits(session, [first, second, third], builder) == (1, 2)
    assert built == [first.id, second.id, third.id]


@pytest.mark.parametrize(
    ("zone", "on", "expected"),
    [
        (EASTERN, date(2026, 10, 7), time(7, 0)),
        (ZoneInfo("America/Los_Angeles"), date(2026, 10, 7), time(4, 0)),
        (UTC, date(2026, 10, 7), time(11, 0)),  # Eastern daylight time
        (UTC, date(2026, 12, 7), time(12, 0)),  # Eastern standard time
    ],
)
def test_start_time_is_converted_to_this_computers_clock(zone, on, expected) -> None:
    assert daily.start_time("09:00", "America/New_York", 2, on=on, local_zone=zone) == expected


def test_windows_task_runs_daily_and_catches_up_after_sleep() -> None:
    command = [r"C:\Projects\Job Agent\.venv\Scripts\python.exe", "-m", "agent.daily"]

    definition = daily.windows_task_xml(time(7, 0), command, Path(r"C:\Projects\Job Agent"))

    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    task = ET.fromstring(definition.split("\n", 1)[1])
    assert task.findtext("t:Triggers/t:CalendarTrigger/t:StartBoundary", namespaces=namespace)
    assert "T07:00:00" in task.findtext(
        "t:Triggers/t:CalendarTrigger/t:StartBoundary", namespaces=namespace
    )
    assert task.findtext("t:Settings/t:StartWhenAvailable", namespaces=namespace) == "true"
    assert task.findtext("t:Settings/t:WakeToRun", namespaces=namespace) == "true"
    assert task.findtext("t:Actions/t:Exec/t:Command", namespaces=namespace) == command[0]
    assert task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=namespace) == "-m agent.daily"


class FakeRunner:
    def __init__(self, crontab: str = "") -> None:
        self.crontab = crontab
        self.calls: list[list[str]] = []
        self.task_definition = ""

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(command)
        if command[:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(command, 0, stdout=self.crontab)
        if command[:2] == ["crontab", "-"]:
            self.crontab = kwargs["input"]
        if "/XML" in command:
            self.task_definition = Path(command[command.index("/XML") + 1]).read_text(
                encoding="utf-16"
            )
        return subprocess.CompletedProcess(command, 0, stdout="")


def test_install_on_windows_registers_a_task_scheduler_task() -> None:
    runner = FakeRunner()

    message = daily.install(load_settings(), system="Windows", runner=runner)

    assert runner.calls[0][:4] == ["schtasks", "/Create", "/TN", daily.TASK_NAME]
    assert "<WakeToRun>true</WakeToRun>" in runner.task_definition
    assert "ready by 09:00 America/New_York" in message


def test_install_with_cron_replaces_only_the_old_morning_run() -> None:
    runner = FakeRunner(f"0 6 * * * old command {daily.CRON_MARKER}\n30 1 * * * backup\n")

    daily.install(load_settings(), system="Linux", runner=runner)

    lines = runner.crontab.splitlines()
    assert lines[0] == "30 1 * * * backup"
    assert len(lines) == 2 and lines[1].endswith(daily.CRON_MARKER)
    assert "'-m' 'agent.daily'" in lines[1]

    daily.uninstall(system="Linux", runner=runner)

    assert runner.crontab == "30 1 * * * backup\n"


def test_uninstall_on_windows_deletes_the_task() -> None:
    runner = FakeRunner()

    daily.uninstall(system="Windows", runner=runner)

    assert runner.calls == [["schtasks", "/Delete", "/TN", daily.TASK_NAME, "/F"]]


def test_run_still_builds_kits_when_discovery_fails(monkeypatch) -> None:
    settings = load_settings()
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        add_job(db, "https://example.com/1")
        db.commit()
    engine.dispose()

    def failing_pipeline(_argv):
        raise RuntimeError("board offline")

    built: list[str] = []
    monkeypatch.setattr("agent.pipeline.main", failing_pipeline)
    monkeypatch.setattr(
        daily, "_kit_builder", lambda _settings: lambda _s, job: built.append(job.url) or Kit()
    )

    assert daily.run(settings, kit_limit=5) == 0
    assert built == ["https://example.com/1"]


def test_daily_run_settings(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        "database_url: 'sqlite+pysqlite:///:memory:'\n"
        "daily_run:\n  ready_by: '08:30'\n  start_hours_before: 1.5\n  kit_limit: 3\n",
        encoding="utf-8",
    )

    settings = load_settings(path, load_env=False)

    assert (settings.daily_ready_by, settings.daily_start_hours_before) == ("08:30", 1.5)
    assert settings.daily_kit_limit == 3
    assert settings.daily_timezone == "America/New_York"

    path.write_text(
        "database_url: 'sqlite+pysqlite:///:memory:'\ndaily_run:\n  ready_by: '9am'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ready_by"):
        load_settings(path, load_env=False)
