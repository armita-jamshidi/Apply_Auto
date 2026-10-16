"""At most four jobs and three projects per tailored resume, each with full bullets."""

import json
from types import SimpleNamespace

from agent import resume_tailor
from agent.library import LibraryDocument
from agent.resume_tailor import Entry, Keywords, TailoredResume

FACT = "Built a search service in Python used by the support team every day."
DOCS = {"master_cv": LibraryDocument("master_cv", "Master CV", FACT)}


def _pool(jobs: int, projects: int) -> dict[str, Entry]:
    entries = [
        Entry(f"j{n}", f"Engineer {n}", f"Example Co {n}", facts=[FACT]) for n in range(jobs)
    ]
    entries += [
        Entry(f"p{n}", f"Project {n}", kind="project", facts=[FACT]) for n in range(projects)
    ]
    return {entry.entry_id: entry for entry in entries}


def _draft(pool: dict[str, Entry]) -> dict:
    bullet = {"text": "Built a search service in Python",
              "evidence": [{"doc_id": "master_cv", "quote": FACT}]}
    return {
        "sections": [{"heading": "Experience", "entries": [
            {"entry_id": entry_id, "bullets": [bullet]} for entry_id in pool
        ]}],
        "skills": [],
    }


def test_only_the_four_most_relevant_jobs_and_three_projects_are_kept() -> None:
    pool = _pool(jobs=6, projects=5)

    sections, _skills, notes, _sources = resume_tailor._keep_verified(
        _draft(pool), Keywords(), pool, DOCS, list(DOCS.values()), max_entries=20
    )

    kept = {heading: [entry.title for entry in entries] for heading, entries in sections}
    assert kept["Experience"] == [f"Engineer {n}" for n in range(4)]  # first in the draft
    assert kept["Technical Projects"] == [f"Project {n}" for n in range(3)]
    assert "Left out Engineer 4: the resume already has 4 jobs." in notes
    assert "Left out Project 3: the resume already has 3 projects." in notes


def test_activities_are_not_capped_by_the_job_or_project_limit() -> None:
    pool = _pool(jobs=4, projects=3)
    pool["a0"] = Entry("a0", "Hack Club Lead", kind="leadership", facts=[FACT])

    sections, *_rest = resume_tailor._keep_verified(
        _draft(pool), Keywords(), pool, DOCS, list(DOCS.values()), max_entries=20
    )

    assert sum(len(entries) for _heading, entries in sections) == 8


def test_the_prompt_states_the_caps() -> None:
    system = resume_tailor.TAILOR_PROMPT.format(
        max_entries=6, headings='"Experience"', max_experiences=4, max_projects=3
    )

    assert "at most 4 jobs" in system and "at most 3\nprojects" in system


def test_a_github_repository_is_always_a_project() -> None:
    repo = LibraryDocument("github:example/agent-demo", "agent-demo", "agent-demo uses langchain")
    payload = {"name": "", "contact": [], "education": [], "entries": [
        {"title": "agent-demo", "organization": "", "location": "", "dates": "",
         "kind": "experience", "doc_id": repo.doc_id, "facts": ["agent-demo uses langchain"]}]}
    messages = SimpleNamespace(create=lambda **_request: SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
    ))
    client = SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))

    _name, _contact, _education, pool = resume_tailor.build_pool([repo], client=client, model="m")

    assert [entry.kind for entry in pool.values()] == ["project"]


def _resume(bullets: list[int]) -> TailoredResume:
    entries = [
        Entry(f"e{n}", f"Engineer {n}", bullets=[f"Built tool {n}.{b}" for b in range(count)])
        for n, count in enumerate(bullets)
    ]
    return TailoredResume("Jordan Example", [], [], [("Experience", entries)], [], Keywords())


def test_trimming_drops_a_thin_entry_before_leaving_entries_with_one_bullet() -> None:
    resume = _resume([3, 2, 2])

    assert resume_tailor._trim_one(resume) == "dropped a bullet from Engineer 0"
    assert resume_tailor._trim_one(resume) == "dropped Engineer 2"
    assert [len(entry.bullets) for entry in resume.sections[0][1]] == [2, 2]


def test_the_last_entry_can_still_go_down_to_one_bullet() -> None:
    resume = _resume([2])

    assert resume_tailor._trim_one(resume) == "dropped a bullet from Engineer 0"
    assert resume_tailor._trim_one(resume) is None
