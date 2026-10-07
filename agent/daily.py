"""job-daily: prepare new jobs each morning, and schedule that run on this computer.

The morning run finds and scores new jobs and reads their forms (job-run without opening the
dashboard). It then builds the apply kit, a tailored resume and an answer for every question,
for each good match that has none yet. Everything is waiting in the dashboard by the
ready-by time in config/settings.yaml. Nothing is submitted.

`job-daily --install` registers the run with Windows Task Scheduler, or with cron on macOS
and Linux, starting start_hours_before hours before the ready-by time.
"""

import argparse
import logging
import platform
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from agent.apply_kit import Kit, latest_kit
from agent.pipeline import WANTED_LOCATIONS, is_fresh
from agent.settings import (
    PROJECT_ROOT,
    AgentSettings,
    excluded_role_reason,
    load_settings,
    title_in_scope,
)
from agent.tracking import READY_FOR_YOU
from db.models import Job

LOGGER = logging.getLogger(__name__)
LOG_PATH = PROJECT_ROOT / "data" / "daily-run.log"
TASK_NAME = "Job Agent morning run"
CRON_MARKER = "# job-agent-daily"
# Stop a run that hangs (a browser that never closes) so it cannot block the next morning.
RUN_TIME_LIMIT = "PT4H"
KitBuilder = Callable[[Session, Job], Kit]
Runner = Callable[..., subprocess.CompletedProcess]


def jobs_needing_kits(session: Session, settings: AgentSettings, *, limit: int) -> list[Job]:
    """Open jobs at or above the fit threshold that have no kit yet, best first.

    Fresh postings come first so you can apply early, then the best fit scores.
    """
    if limit <= 0:
        return []
    jobs = session.scalars(
        select(Job)
        .options(selectinload(Job.applications))
        .where(
            Job.status.in_(("new", READY_FOR_YOU)),
            Job.location_category.in_(WANTED_LOCATIONS),
            Job.fit_score >= settings.fit_score_threshold,
        )
        .order_by(Job.fit_score.desc(), Job.first_seen_at.desc(), Job.id)
    ).all()
    wanted = [
        job
        for job in jobs
        if latest_kit(job) is None
        and not job.dealbreakers
        and job.fit_recommendation in (None, "apply")
        and excluded_role_reason(job.title, job.description, settings) is None
        and title_in_scope(job.title, settings)
    ]
    fresh_since = datetime.now(UTC) - timedelta(days=settings.fresh_posting_days)
    wanted.sort(key=lambda job: not is_fresh(job.posted_at, fresh_since))
    return wanted[:limit]


def build_kits(session: Session, jobs: list[Job], builder: KitBuilder) -> tuple[int, int]:
    """Build each job's kit; return (complete kits, kits with problems).

    One job's failure is logged and the rest still get their kits.
    """
    complete = with_problems = 0
    for index, job in enumerate(jobs, start=1):
        print(f"\nKit {index}/{len(jobs)}: {job.company} - {job.title} (fit {job.fit_score})")
        try:
            kit = builder(session, job)
        except Exception:
            LOGGER.exception("Could not build the kit for job %s", job.id)
            session.rollback()
            with_problems += 1
            continue
        if kit.problems:
            with_problems += 1
            for problem in kit.problems:
                print(f"Problem: {problem}")
        else:
            complete += 1
    return complete, with_problems


def start_time(
    ready_by: str,
    timezone: str,
    hours_before: float,
    *,
    on: date | None = None,
    local_zone: tzinfo | None = None,
) -> time:
    """The local clock time to start so the run is done by ready_by in timezone.

    Schedulers use this computer's clock, so the time is converted from timezone (Eastern by
    default) to the computer's own zone.
    """
    hour, minute = (int(part) for part in ready_by.split(":"))
    ready = datetime.combine(on or date.today(), time(hour, minute), tzinfo=ZoneInfo(timezone))
    start = ready - timedelta(hours=hours_before)
    local = start.astimezone(local_zone) if local_zone else start.astimezone()
    return local.time().replace(second=0, microsecond=0, tzinfo=None)


def run_command() -> list[str]:
    """The command the scheduler runs: this Python, so the project's own environment is used."""
    return [sys.executable, "-m", "agent.daily"]


def windows_task_xml(start: time, command: list[str], workdir: Path) -> str:
    """A Task Scheduler definition that runs every morning, even after the computer slept.

    StartWhenAvailable runs a missed start as soon as the computer is on, and WakeToRun wakes
    a sleeping computer for it.
    """
    program, *arguments = command
    begins = datetime.combine(date.today(), start).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Find, score, and prepare new jobs before the morning.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{begins}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>{RUN_TIME_LIMIT}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(subprocess.list2cmdline(arguments))}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def cron_line(start: time, command: list[str], workdir: Path) -> str:
    """A crontab line that runs every morning at start, marked so it can be replaced."""
    quoted = " ".join(_shell_quote(part) for part in command)
    folder = _shell_quote(str(workdir))
    return f"{start.minute} {start.hour} * * * cd {folder} && {quoted} {CRON_MARKER}"


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def install(
    settings: AgentSettings,
    *,
    system: str | None = None,
    runner: Runner = subprocess.run,
) -> str:
    """Register the morning run with this computer's scheduler; return what was set up."""
    start = start_time(
        settings.daily_ready_by, settings.daily_timezone, settings.daily_start_hours_before
    )
    command = run_command()
    if (system or platform.system()) == "Windows":
        with tempfile.TemporaryDirectory() as folder:
            definition = Path(folder) / "task.xml"
            definition.write_text(
                windows_task_xml(start, command, PROJECT_ROOT), encoding="utf-16"
            )
            runner(
                ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(definition), "/F"],
                check=True,
            )
        where = "Windows Task Scheduler"
    else:
        lines = [line for line in _crontab(runner) if CRON_MARKER not in line]
        lines.append(cron_line(start, command, PROJECT_ROOT))
        runner(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
        where = "cron"
    return (
        f"The morning run starts at {start:%H:%M} on this computer's clock ({where}), "
        f"so new jobs are ready by {settings.daily_ready_by} {settings.daily_timezone}."
    )


def uninstall(*, system: str | None = None, runner: Runner = subprocess.run) -> str:
    """Remove the morning run from this computer's scheduler."""
    if (system or platform.system()) == "Windows":
        runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False)
    else:
        lines = [line for line in _crontab(runner) if CRON_MARKER not in line]
        remaining = "\n".join(lines) + "\n" if lines else ""
        runner(["crontab", "-"], input=remaining, text=True, check=True)
    return "The morning run is no longer scheduled."


def _crontab(runner: Runner) -> list[str]:
    """The current crontab's lines (none when the user has no crontab yet)."""
    listed = runner(["crontab", "-l"], capture_output=True, text=True, check=False)
    return [line for line in (listed.stdout or "").splitlines() if line.strip()]


def run(settings: AgentSettings, *, skip_discovery: bool = False, kit_limit: int) -> int:
    """Find, score, and read forms for new jobs, then build their kits; refresh the dashboard."""
    from agent import pipeline
    from agent.dashboard import refresh_dashboard
    from db.session import create_database_engine, create_session_factory, ensure_schema

    started = datetime.now()
    LOGGER.info("Morning run started")
    pipeline_args = ["--no-open", "--prepare", str(kit_limit)]
    try:
        pipeline.main(pipeline_args + (["--skip-discovery"] if skip_discovery else []))
    except Exception:
        # Jobs found on earlier days still get their kits.
        LOGGER.exception("Finding and scoring new jobs failed; preparing the jobs already found")

    builder = _kit_builder(settings)
    engine = create_database_engine(settings.database_url)
    try:
        ensure_schema(engine)
        with create_session_factory(engine)() as session:
            jobs = jobs_needing_kits(session, settings, limit=kit_limit)
            complete, with_problems = build_kits(session, jobs, builder)
    finally:
        engine.dispose()
    refresh_dashboard()
    minutes = (datetime.now() - started).total_seconds() / 60
    summary = (
        f"Morning run finished in {minutes:.0f} minutes: {complete} kits ready, "
        f"{with_problems} with problems."
    )
    LOGGER.info(summary)
    print(f"\n{summary}")
    return 0 if not with_problems else 2


def _kit_builder(settings: AgentSettings) -> KitBuilder:
    """Build kits with no console questions: nobody is at the keyboard in the morning."""
    from agent.answers import _create_client
    from agent.applier.cli import default_resume_path
    from agent.applier.greenhouse import extract_resume_text, load_profile
    from agent.apply_kit import build_kit
    from agent.pipeline import PROFILE_PATH
    from agent.resume_format import load_resume_sources

    profile: dict[str, Any] = load_profile(PROFILE_PATH)
    resume_text = extract_resume_text(default_resume_path())
    sources = load_resume_sources()
    client = _create_client()

    def build(session: Session, job: Job) -> Kit:
        return build_kit(
            session,
            job,
            profile=profile,
            resume_text=resume_text,
            client=client,
            answer_model=settings.anthropic_model,
            resume_model=settings.resume_tailor_model,
            ask=None,
            sources=sources,
        )

    return build


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare new jobs (fit score, tailored resume, every answer) before the morning. "
            "Never submits an application."
        )
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--install", action="store_true", help="Run this every morning on this computer"
    )
    action.add_argument(
        "--uninstall", action="store_true", help="Stop running this every morning"
    )
    parser.add_argument("--skip-discovery", action="store_true", help="Use jobs already found")
    parser.add_argument(
        "--kit-limit",
        type=int,
        metavar="N",
        help="Most new kits to build (default: daily_run.kit_limit in settings.yaml)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.install:
        print(install(settings))
        return 0
    if args.uninstall:
        print(uninstall())
        return 0
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )
    limit = settings.daily_kit_limit if args.kit_limit is None else args.kit_limit
    return run(settings, skip_discovery=args.skip_discovery, kit_limit=limit)


if __name__ == "__main__":
    raise SystemExit(main())
