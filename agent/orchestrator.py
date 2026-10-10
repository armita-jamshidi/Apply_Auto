"""job-run --agent: one orchestrator agent that works toward a goal instead of a fixed script.

The goal is a number of strong kits (a tailored resume and every answer, with no problems).
The model sees where things stand and what the candidate decided before (applied, removed,
earlier runs), then picks its own next step from a small set of tools: find new jobs, look
up company pages, score jobs, list the best candidates, read a job's form, build a kit.

Code still decides what is allowed:

- The hard filters (location, years of experience, internships, titles, fit threshold,
  jobs per company) run in code. The model can only read forms and build kits for jobs that
  code listed as candidates in this run.
- Every tool has a budget (one discovery per run, a scoring limit, a form-reading limit, a
  step limit), so a run cannot loop or spend without bound.
- There is no submit tool. The candidate still submits every application.

Each run is appended to data/agent-runs.jsonl (git-ignored): the goal, each step with its
time, the orchestrator's own token use, the kits built, and why the run stopped. Earlier
runs from that log are the agent's memory, with the job statuses in the database.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent.apply_kit import Kit, form_was_read, latest_kit
from agent.daily import jobs_needing_kits
from agent.settings import PROJECT_ROOT, AgentSettings
from agent.tracking import READY_FOR_YOU, REMOVED
from db.models import Job

LOGGER = logging.getLogger(__name__)
RUN_LOG_PATH = PROJECT_ROOT / "data" / "agent-runs.jsonl"
CANDIDATE_LIMIT = 20
RECENT_RUNS = 3
RECENT_DECISIONS = 20

SYSTEM_PROMPT = """You run a job search for one candidate. Your goal is a number of strong \
application kits (a tailored resume and an answer to every question, with no problems), \
ready for the candidate to review and submit themselves.

Each turn, look at what you know and call the one tool that moves the goal forward most. \
Typical order: check the status; score jobs if good unscored jobs may exist; find new jobs \
only when there are not enough candidates; read a candidate's application form before its \
kit, so the kit answers the form's real questions; build kits for the best candidates. \
Prefer fresh postings and high fit scores. Use the candidate's past decisions: avoid roles \
like the ones they removed, and do not crowd the list with one company.

Tools have budgets. When a tool says a budget is spent or a job is not allowed, do not \
retry it. Call finish when the goal is met or nothing useful is left, with a one-sentence \
summary in plain words."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_status",
        "description": "Counts of jobs by stage and progress toward the goal.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_new_jobs",
        "description": (
            "Search every job source for new postings, link them to the companies' own "
            "applications, hide closed postings, and remove jobs the filters rule out. "
            "Slow; allowed once per run."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_company_pages",
        "description": (
            "Look up jobs found on third-party boards on the companies' own careers pages, "
            "so their real titles and forms are used. Allowed once per run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        },
    },
    {
        "name": "score_jobs",
        "description": (
            "Fit-score up to `limit` unscored jobs (one model call each). Jobs with unmet "
            "requirements or a low score are skipped automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1}},
            "required": ["limit"],
        },
    },
    {
        "name": "list_candidates",
        "description": (
            "The best scored jobs that pass every filter and have no kit yet, fresh postings "
            "first. Only these jobs can have their form read or a kit built."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": CANDIDATE_LIMIT}},
        },
    },
    {
        "name": "read_application_form",
        "description": (
            "Open a candidate job's application form in a headless browser and record every "
            "question. Never submits. Slow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "integer"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "build_kit",
        "description": (
            "Write the tailored one-page resume and an answer to every question for one "
            "candidate job. Returns whether the kit has problems and how many required "
            "qualifications the resume hits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "integer"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "finish",
        "description": "End the run.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]


@dataclass(frozen=True)
class Goal:
    """What one run aims for, and the budgets that bound it."""

    kits: int = 5
    max_steps: int = 40
    max_scores: int = 150
    max_form_reads: int = 10


@dataclass
class Actions:
    """The work behind each tool, passed in so tests never touch the network or a browser."""

    discover: Callable[[Session], dict[str, int]]
    check_pages: Callable[[Session, int], int]
    score: Callable[[Session, int], int]
    read_form: Callable[[Job], int]
    build_kit: Callable[[Session, Job], Kit]


@dataclass
class Usage:
    """The orchestrator's own token use (the sub-agents it calls are not counted here)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    requests: int = 0

    def add(self, usage: Any) -> None:
        self.requests += 1
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            setattr(self, name, getattr(self, name) + int(getattr(usage, name, 0) or 0))


@dataclass
class RunLog:
    """One run, as written to the run log."""

    started_at: str
    goal: dict[str, int]
    model: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    kits_ready: list[int] = field(default_factory=list)
    kits_with_problems: list[int] = field(default_factory=list)
    stopped_because: str = ""
    summary: str = ""
    seconds: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def recent_runs(path: Path = RUN_LOG_PATH, count: int = RECENT_RUNS) -> list[dict[str, Any]]:
    """The last few runs from the run log, short form; none when there is no log yet."""
    if not path.exists():
        return []
    runs: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            continue
        runs.append(
            {
                "started_at": run.get("started_at"),
                "kits_ready": len(run.get("kits_ready") or []),
                "kits_with_problems": len(run.get("kits_with_problems") or []),
                "stopped_because": run.get("stopped_because"),
                "summary": run.get("summary"),
            }
        )
    return runs[-count:]


def past_decisions(session: Session, log_path: Path = RUN_LOG_PATH) -> dict[str, Any]:
    """What the candidate decided before: jobs applied to, jobs they removed, recent runs.

    A job the candidate removed on the dashboard has no reason attached; jobs the filters
    removed always do, so those are left out.
    """

    def recent(*conditions: Any) -> list[Job]:
        return list(
            session.scalars(
                select(Job).where(*conditions).order_by(Job.id.desc()).limit(RECENT_DECISIONS)
            ).all()
        )

    def named(jobs: list[Job]) -> list[dict[str, str]]:
        return [{"company": job.company, "title": job.title} for job in jobs]

    removed = [job for job in recent(Job.status == REMOVED) if not job.dealbreakers]
    return {
        "applied": named(recent(Job.status == "applied")),
        "removed_by_candidate": named(removed[:RECENT_DECISIONS]),
        "recent_runs": recent_runs(log_path),
    }


class Orchestrator:
    """One run toward a goal; see the module docstring."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        session: Session,
        settings: AgentSettings,
        goal: Goal,
        actions: Actions,
        log_path: Path = RUN_LOG_PATH,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.model = model
        self.session = session
        self.settings = settings
        self.goal = goal
        self.actions = actions
        self.log_path = log_path
        self.clock = clock
        self.log = RunLog(
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            goal=asdict(goal),
            model=model,
        )
        self.allowed: set[int] = set()
        self.used: set[str] = set()
        self.scored = 0
        self.forms_read = 0

    def run(self) -> RunLog:
        """Let the model pick steps until it finishes, the goal is met, or a budget runs out."""
        started = self.clock()
        facts = {
            "goal": f"{self.goal.kits} strong kits",
            "budgets": asdict(self.goal),
            "status": self._status(),
            "past_decisions": past_decisions(self.session, self.log_path),
        }
        messages: list[Any] = [
            {"role": "user", "content": json.dumps(facts) + "\n\nWork toward the goal."}
        ]
        try:
            self.log.stopped_because = self._loop(messages)
        finally:
            self.log.seconds = round(self.clock() - started, 1)
            self._write_log()
        return self.log

    def _loop(self, messages: list[Any]) -> str:
        steps = 0
        while steps < self.goal.max_steps:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                tool_choice={"type": "auto"},
                messages=messages,
            )
            self.log.usage.add(getattr(response, "usage", None))
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if response.stop_reason in {"refusal", "max_tokens"} or not calls:
                return f"The model stopped ({response.stop_reason})."
            results = []
            for call in calls:
                steps += 1
                arguments = call.input if isinstance(call.input, dict) else {}
                if call.name == "finish":
                    self.log.summary = str(arguments.get("summary", ""))[:500]
                    return "Finished."
                began = self.clock()
                try:
                    output = self._run_tool(call.name, arguments)
                except Exception as error:  # one failed step does not end the run
                    LOGGER.exception("The %s step failed", call.name)
                    self.session.rollback()
                    output = {"error": f"{call.name} failed: {type(error).__name__}"}
                self.log.steps.append(
                    {
                        "tool": call.name,
                        "input": arguments,
                        "seconds": round(self.clock() - began, 1),
                        "result": _short(output),
                    }
                )
                print(f"[agent] {call.name} {json.dumps(arguments)} -> {_short(output)}")
                results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(output)}
                )
            messages.append({"role": "user", "content": results})
            if len(self.log.kits_ready) >= self.goal.kits:
                return "The goal is met."
        return f"Reached the step limit ({self.goal.max_steps})."

    def _run_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == "get_status":
            return self._status()
        if name == "find_new_jobs":
            if not self._first_use(name):
                return {"error": "Already searched in this run."}
            counts = self.actions.discover(self.session)
            return {"done": True, **counts, "status": self._status()}
        if name == "check_company_pages":
            if not self._first_use(name):
                return {"error": "Already checked company pages in this run."}
            default = self.settings.careers_agent_limit
            limit = _bounded(arguments.get("limit"), 1, 20, default=default)
            return {"updated": self.actions.check_pages(self.session, limit)}
        if name == "score_jobs":
            remaining = self.goal.max_scores - self.scored
            if remaining <= 0:
                return {"error": "The scoring budget for this run is spent."}
            limit = _bounded(arguments.get("limit"), 1, remaining, default=remaining)
            scored = self.actions.score(self.session, limit)
            self.scored += scored
            return {"scored": scored, "scoring_budget_left": self.goal.max_scores - self.scored}
        if name == "list_candidates":
            limit = _bounded(arguments.get("limit"), 1, CANDIDATE_LIMIT, default=10)
            jobs = jobs_needing_kits(self.session, self.settings, limit=limit)
            self.allowed.update(job.id for job in jobs)
            return {"candidates": [_candidate(job) for job in jobs]}
        if name == "read_application_form":
            job = self._candidate_job(arguments)
            if isinstance(job, dict):
                return job
            if form_was_read(job):
                return {"error": "This job's form was already read."}
            if self.forms_read >= self.goal.max_form_reads:
                return {"error": "The form-reading budget for this run is spent."}
            self.forms_read += 1
            code = self.actions.read_form(job)
            self.session.expire_all()  # the dry run wrote in its own process
            return {"read": code in (0, 2), "exit_code": code}
        if name == "build_kit":
            job = self._candidate_job(arguments)
            if isinstance(job, dict):
                return job
            if latest_kit(job) is not None:
                return {"error": "This job already has a kit."}
            kit = self.actions.build_kit(self.session, job)
            (self.log.kits_with_problems if kit.problems else self.log.kits_ready).append(job.id)
            required = [row for row in kit.qualification_map if row.get("kind") == "required"]
            return {
                "ok": not kit.problems,
                "problems": len(kit.problems),
                "required_hit": sum(bool(row.get("matches")) for row in required),
                "required_total": len(required),
                "kits_ready": len(self.log.kits_ready),
                "goal": self.goal.kits,
            }
        return {"error": f"Unknown tool {name!r}."}

    def _first_use(self, name: str) -> bool:
        if name in self.used:
            return False
        self.used.add(name)
        return True

    def _candidate_job(self, arguments: Mapping[str, Any]) -> Job | dict[str, str]:
        """The job for job_id when list_candidates offered it in this run."""
        job_id = arguments.get("job_id")
        if not isinstance(job_id, int) or job_id not in self.allowed:
            return {"error": "Only jobs from list_candidates in this run are allowed."}
        job = self.session.get(Job, job_id)
        if job is None:
            return {"error": "No such job."}
        return job

    def _status(self) -> dict[str, Any]:
        def count(*conditions: Any) -> int:
            return int(self.session.scalar(select(func.count(Job.id)).where(*conditions)) or 0)

        return {
            "unscored": count(Job.status == "new", Job.fit_score.is_(None)),
            "good_matches": count(
                Job.status.in_(("new", READY_FOR_YOU)),
                Job.fit_score >= self.settings.fit_score_threshold,
            ),
            "kits_ready_this_run": len(self.log.kits_ready),
            "kits_with_problems_this_run": len(self.log.kits_with_problems),
            "goal": self.goal.kits,
            "scoring_budget_left": self.goal.max_scores - self.scored,
            "form_reads_left": self.goal.max_form_reads - self.forms_read,
        }

    def _write_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(self.log.to_json() + "\n")


def _candidate(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "fit_score": job.fit_score,
        "location": job.location_category,
        "posted": job.posted_at.date().isoformat() if job.posted_at else None,
        "form_read": form_was_read(job),
    }


def _bounded(value: Any, low: int, high: int, *, default: int) -> int:
    number = value if isinstance(value, int) and not isinstance(value, bool) else default
    return max(low, min(high, number))


def _short(output: Mapping[str, Any]) -> str:
    """A one-line result for the console and the log, without job lists."""
    shown = {key: value for key, value in output.items() if key not in ("candidates", "status")}
    if "candidates" in output:
        shown["candidates"] = len(output["candidates"])
    return json.dumps(shown)[:200]


def default_actions(settings: AgentSettings, profile: Mapping[str, Any]) -> Actions:
    """The real work behind each tool: the same steps job-run and job-daily use."""
    from agent import pipeline
    from agent.careers_agent import check_company_pages
    from agent.daily import _kit_builder
    from agent.main import main as discover
    from agent.postings import close_finished_postings

    builder: Callable[[Session, Job], Kit] | None = None

    def find(session: Session) -> dict[str, int]:
        discover(["--no-dashboard"])
        session.expire_all()  # discovery wrote with its own session
        return {
            "linked_to_company_site": pipeline.resolve_application_links(session),
            "closed": close_finished_postings(session),
            "removed_by_filters": pipeline.remove_out_of_scope_jobs(session, settings),
            "removed_over_company_limit": pipeline.cap_jobs_per_company(session, settings),
        }

    def check_pages(session: Session, limit: int) -> int:
        return check_company_pages(session, model=settings.careers_agent_model, agent_limit=limit)

    def score(session: Session, limit: int) -> int:
        scored = pipeline.score_unscored_jobs(session, profile, settings, limit=limit)
        pipeline.cap_jobs_per_company(session, settings)
        return scored

    def read_form(job: Job) -> int:
        return pipeline.run_prepare(pipeline.prepare_command(job))

    def build_kit(session: Session, job: Job) -> Kit:
        nonlocal builder
        if builder is None:  # loads the resume and sources only when a kit is wanted
            builder = _kit_builder(settings)
        return builder(session, job)

    return Actions(
        discover=find,
        check_pages=check_pages,
        score=score,
        read_form=read_form,
        build_kit=build_kit,
    )


def run_agent(settings: AgentSettings, profile: Mapping[str, Any], goal: Goal) -> RunLog:
    """Run the orchestrator against the local database with the real tools."""
    from agent.answers import _create_client
    from db.session import create_database_engine, create_session_factory, ensure_schema

    client = _create_client()
    engine = create_database_engine(settings.database_url)
    try:
        ensure_schema(engine)
        with create_session_factory(engine)() as session:
            orchestrator = Orchestrator(
                client=client,
                model=settings.orchestrator_model,
                session=session,
                settings=settings,
                goal=goal,
                actions=default_actions(settings, profile),
            )
            log = orchestrator.run()
    finally:
        engine.dispose()
    usage = log.usage
    print(
        f"\nAgent run: {len(log.kits_ready)} of {goal.kits} kits ready, "
        f"{len(log.kits_with_problems)} with problems, {len(log.steps)} steps, "
        f"{log.seconds / 60:.0f} minutes, {usage.input_tokens + usage.output_tokens} "
        f"orchestrator tokens. {log.stopped_because}"
    )
    if log.summary:
        print(log.summary)
    print(f"Run log: {RUN_LOG_PATH}")
    return log
