"""Tests for the orchestrator agent (job-run --agent), with a scripted model and fake tools."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import orchestrator, pipeline
from agent.apply_kit import Kit, record_kit
from agent.orchestrator import TOOLS, Actions, Goal, Orchestrator, past_decisions, recent_runs
from agent.settings import load_settings
from db.models import Base, Job


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


def call(name: str, arguments: dict, number: int) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=arguments, id=f"call{number}")


def turn(*calls: SimpleNamespace, stop: str = "tool_use", tokens: int = 100) -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=tokens, output_tokens=tokens // 10)
    return SimpleNamespace(stop_reason=stop, content=list(calls), usage=usage)


class ScriptedModel:
    """Returns one prepared response per request and records the requests."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request):
        # The agent keeps adding to one messages list; keep what this request saw.
        self.requests.append({**request, "messages": list(request["messages"])})
        return self.responses.pop(0)


class FakeActions:
    """Records each tool's work instead of touching the network, a browser, or a model."""

    def __init__(self, *, kit_problems: dict[int, list[str]] | None = None) -> None:
        self.calls: list[tuple] = []
        self.kit_problems = kit_problems or {}

    def actions(self) -> Actions:
        return Actions(
            discover=lambda _s: self.calls.append(("discover",)) or {"removed_by_filters": 0},
            check_pages=lambda _s, limit: self.calls.append(("pages", limit)) or 0,
            score=lambda _s, limit: self.calls.append(("score", limit)) or limit,
            read_form=lambda job: self.calls.append(("form", job.id)) or 0,
            build_kit=self.build_kit,
        )

    def build_kit(self, session: Session, job: Job) -> Kit:
        self.calls.append(("kit", job.id))
        kit = Kit(
            problems=list(self.kit_problems.get(job.id, [])),
            qualification_map=[
                {"kind": "required", "matches": [{"bullet": "x"}]},
                {"kind": "required", "matches": []},
                {"kind": "preferred", "matches": []},
            ],
        )
        record_kit(session, job, kit)
        return kit


def make(session, model, fake, tmp_path, **goal) -> Orchestrator:
    return Orchestrator(
        client=model,
        model="test-model",
        session=session,
        settings=load_settings(),
        goal=Goal(**goal),
        actions=fake.actions(),
        log_path=tmp_path / "agent-runs.jsonl",
    )


def results(model: ScriptedModel, request: int) -> list[dict]:
    """The tool results the model received in a given request."""
    return [
        json.loads(item["content"]) for item in model.requests[request]["messages"][-1]["content"]
    ]


def test_there_is_no_submit_tool() -> None:
    names = {tool["name"] for tool in TOOLS}
    assert not any(
        "submit" in name or "apply" in name for name in names - {"read_application_form"}
    )


def test_the_agent_picks_steps_and_stops_when_the_goal_is_met(session, tmp_path) -> None:
    first = add_job(session, "https://example.com/1", fit_score=90)
    second = add_job(session, "https://example.com/2", fit_score=85)
    session.commit()
    model = ScriptedModel(
        [
            turn(call("score_jobs", {"limit": 10}, 1)),
            turn(call("list_candidates", {"limit": 5}, 2)),
            turn(call("read_application_form", {"job_id": first.id}, 3)),
            turn(
                call("build_kit", {"job_id": first.id}, 4),
                call("build_kit", {"job_id": second.id}, 5),
            ),
        ]
    )
    fake = FakeActions()

    log = make(session, model, fake, tmp_path, kits=2).run()

    assert fake.calls == [("score", 10), ("form", first.id), ("kit", first.id), ("kit", second.id)]
    assert log.kits_ready == [first.id, second.id]
    assert log.stopped_because == "The goal is met."
    candidates = results(model, 2)[0]["candidates"]
    assert [c["job_id"] for c in candidates] == [first.id, second.id]
    assert results(model, 3) == [{"read": True, "exit_code": 0}]
    # The model sees where things stand and what was decided before, in the first request.
    facts = json.loads(model.requests[0]["messages"][0]["content"].split("\n\n")[0])
    assert facts["status"]["good_matches"] == 2
    assert set(facts["past_decisions"]) == {"applied", "removed_by_candidate", "recent_runs"}


def test_kits_only_for_jobs_code_listed_as_candidates(session, tmp_path) -> None:
    good = add_job(session, "https://example.com/1", fit_score=90)
    filtered = add_job(session, "https://example.com/2", dealbreakers=["Needs 8+ years"])
    unlisted = add_job(session, "https://example.com/3")
    session.commit()
    model = ScriptedModel(
        [
            turn(call("list_candidates", {"limit": 1}, 1)),
            turn(
                call("build_kit", {"job_id": filtered.id}, 2),
                call("build_kit", {"job_id": unlisted.id}, 3),
                call("read_application_form", {"job_id": "1"}, 4),
            ),
            turn(call("build_kit", {"job_id": good.id}, 5)),
            turn(call("build_kit", {"job_id": good.id}, 6)),
            turn(call("finish", {"summary": "One kit is ready."}, 7)),
        ]
    )
    fake = FakeActions()

    log = make(session, model, fake, tmp_path, kits=5).run()

    assert fake.calls == [("kit", good.id)]
    refused = results(model, 2)
    assert all("Only jobs from list_candidates" in item["error"] for item in refused)
    assert results(model, 3)[0]["required_hit"] == 1 and results(model, 3)[0]["required_total"] == 2
    assert results(model, 4) == [{"error": "This job already has a kit."}]
    assert (log.stopped_because, log.summary) == ("Finished.", "One kit is ready.")


def test_budgets_are_enforced_in_code(session, tmp_path) -> None:
    job = add_job(session, "https://example.com/1")
    session.commit()
    model = ScriptedModel(
        [
            turn(call("find_new_jobs", {}, 1), call("find_new_jobs", {}, 2)),
            turn(call("score_jobs", {"limit": 500}, 3), call("score_jobs", {"limit": 1}, 4)),
            turn(call("list_candidates", {}, 5)),
            turn(call("read_application_form", {"job_id": job.id}, 6)),
            turn(call("get_status", {}, 7)),
        ]
    )
    fake = FakeActions()

    log = make(session, model, fake, tmp_path, max_scores=20, max_form_reads=0, max_steps=7).run()

    assert fake.calls == [("discover",), ("score", 20)]
    assert results(model, 1)[1] == {"error": "Already searched in this run."}
    assert results(model, 2)[1] == {"error": "The scoring budget for this run is spent."}
    assert results(model, 4) == [{"error": "The form-reading budget for this run is spent."}]
    assert log.stopped_because == "Reached the step limit (7)."


def test_a_failed_step_is_reported_and_the_run_goes_on(session, tmp_path) -> None:
    job = add_job(session, "https://example.com/1")
    session.commit()
    model = ScriptedModel(
        [
            turn(call("list_candidates", {}, 1)),
            turn(call("build_kit", {"job_id": job.id}, 2)),
            turn(call("finish", {"summary": "Stopped."}, 3)),
        ]
    )
    fake = FakeActions()

    def broken(_session, _job):
        raise RuntimeError("the resume sub-agent crashed")

    agent = make(session, model, fake, tmp_path)
    agent.actions.build_kit = broken

    log = agent.run()

    assert results(model, 2) == [{"error": "build_kit failed: RuntimeError"}]
    assert log.stopped_because == "Finished."


def test_each_run_is_logged_with_steps_tokens_and_time(session, tmp_path) -> None:
    job = add_job(session, "https://example.com/1")
    session.commit()
    model = ScriptedModel(
        [
            turn(call("list_candidates", {}, 1), tokens=1000),
            turn(call("build_kit", {"job_id": job.id}, 2), tokens=2000),
        ]
    )
    ticks = iter(range(100))
    agent = make(session, model, FakeActions(kit_problems={job.id: ["Answers: timeout"]}), tmp_path)
    agent.clock = lambda: float(next(ticks))
    agent.goal = Goal(kits=1, max_steps=2)

    agent.run()

    run = json.loads((tmp_path / "agent-runs.jsonl").read_text(encoding="utf-8"))
    assert run["usage"]["input_tokens"] == 3000 and run["usage"]["output_tokens"] == 300
    assert run["usage"]["requests"] == 2
    assert [step["tool"] for step in run["steps"]] == ["list_candidates", "build_kit"]
    assert all(step["seconds"] == 1.0 for step in run["steps"])
    assert run["seconds"] > 0
    assert run["kits_with_problems"] == [job.id] and run["kits_ready"] == []
    # A kit with problems does not count toward the goal.
    assert run["stopped_because"] == "Reached the step limit (2)."
    assert recent_runs(tmp_path / "agent-runs.jsonl") == [
        {
            "started_at": run["started_at"],
            "kits_ready": 0,
            "kits_with_problems": 1,
            "stopped_because": "Reached the step limit (2).",
            "summary": "",
        }
    ]


def test_memory_holds_applied_and_candidate_removed_jobs(session, tmp_path) -> None:
    add_job(session, "https://example.com/1", company="Applied Co", status="applied")
    add_job(session, "https://example.com/2", company="Removed Co", status="removed")
    add_job(
        session,
        "https://example.com/3",
        company="Filtered Co",
        status="removed",
        dealbreakers=["Needs 8+ years"],
    )
    session.commit()

    memory = past_decisions(session, tmp_path / "missing.jsonl")

    assert memory["applied"] == [{"company": "Applied Co", "title": "AI Engineer"}]
    assert memory["removed_by_candidate"] == [{"company": "Removed Co", "title": "AI Engineer"}]
    assert memory["recent_runs"] == []


def test_job_run_agent_flag_runs_the_orchestrator(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(pipeline, "load_profile", lambda _path: {})
    monkeypatch.setattr(
        orchestrator, "run_agent", lambda _settings, _profile, goal: seen.setdefault("goal", goal)
    )
    monkeypatch.setattr(pipeline, "refresh_dashboard", lambda: None)
    monkeypatch.setattr(pipeline, "discover", lambda _argv: pytest.fail("no fixed script"))

    assert pipeline.main(["--agent", "--goal", "3", "--score-limit", "40", "--no-open"]) == 0
    assert seen["goal"] == Goal(kits=3, max_scores=40)
