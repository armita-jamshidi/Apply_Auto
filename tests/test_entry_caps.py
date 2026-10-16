"""Tailored resumes: at most four jobs and three projects, at least two bullets each, the
current job and its pinned bullets always on, and the page filled."""

import json
from types import SimpleNamespace

from agent import resume_tailor
from agent.library import LibraryDocument
from agent.resume_tailor import Entry, Keywords, Pin, TailoredResume

FACTS = [
    "Built a search service in Python used by the support team every day.",
    "Ran 900 interviews with customers to shape the roadmap.",
    "Raised $100K in funding from two local investors.",
]
DOCS = {"master_cv": LibraryDocument("master_cv", "Master CV", "\n".join(FACTS))}
BULLET = {"text": "Built a search service in Python",
          "evidence": [{"doc_id": "master_cv", "quote": FACTS[0]}]}
OTHER = {"text": "Ran 900 interviews with customers",
         "evidence": [{"doc_id": "master_cv", "quote": FACTS[1]}]}


def _entry(entry_id: str, title: str, **fields) -> Entry:
    return Entry(entry_id, title, doc_id="master_cv", facts=list(FACTS), **fields)


def _pool(jobs: int, projects: int) -> dict[str, Entry]:
    entries = [_entry(f"j{n}", f"Engineer {n}", organization=f"Example Co {n}")
               for n in range(jobs)]
    entries += [_entry(f"p{n}", f"Project {n}", kind="project") for n in range(projects)]
    return {entry.entry_id: entry for entry in entries}


def _draft(entry_ids, bullets=(BULLET, OTHER)) -> dict:
    return {
        "sections": [{"heading": "Experience", "entries": [
            {"entry_id": entry_id, "bullets": list(bullets)} for entry_id in entry_ids
        ]}],
        "skills": [],
    }


def _keep(draft, pool, pinned=None):
    return resume_tailor._keep_verified(
        draft, Keywords(), pool, DOCS, list(DOCS.values()), max_entries=20, pinned=pinned
    )


def _titles(sections) -> dict[str, list[str]]:
    return {heading: [entry.title for entry in entries] for heading, entries in sections}


def test_only_the_four_most_relevant_jobs_and_three_projects_are_kept() -> None:
    pool = _pool(jobs=6, projects=5)

    sections, _skills, notes, _sources = _keep(_draft(pool), pool)

    kept = _titles(sections)
    assert kept["Experience"] == [f"Engineer {n}" for n in range(4)]  # first in the draft
    assert kept["Technical Projects"] == [f"Project {n}" for n in range(3)]
    assert "Left out Engineer 4: the resume already has 4 jobs." in notes
    assert "Left out Project 3: the resume already has 3 projects." in notes


def test_activities_are_not_capped_by_the_job_or_project_limit() -> None:
    pool = _pool(jobs=4, projects=3)
    pool["a0"] = _entry("a0", "Hack Club Lead", kind="leadership")

    sections, *_rest = _keep(_draft(pool), pool)

    assert sum(len(entries) for _heading, entries in sections) == 8


def test_a_one_bullet_entry_gets_a_second_from_its_own_facts() -> None:
    pool = _pool(jobs=1, projects=0)

    sections, _skills, notes, sources = _keep(_draft(pool, [BULLET]), pool)

    (entry,) = sections[0][1]
    assert entry.bullets == [BULLET["text"], FACTS[1]]  # not FACTS[0]: the bullet says it
    assert sources[FACTS[1]] == [("master_cv", FACTS[1])]
    assert "Engineer 0: used your original wording for 1 bullet(s)." in notes


def test_an_entry_that_cannot_reach_two_bullets_is_left_out() -> None:
    pool = _pool(jobs=2, projects=0)
    pool["j1"].facts = [FACTS[0]]

    sections, _skills, notes, _sources = _keep(_draft(pool, [BULLET]), pool)

    assert _titles(sections) == {"Experience": ["Engineer 0"]}
    assert "Left out Engineer 1: it had fewer than 2 bullets." in notes


def test_the_current_job_is_pinned_and_a_profile_pin_keeps_its_bullets() -> None:
    pool = _pool(jobs=2, projects=0)
    pool["j1"].dates = "2025 - Present"
    pins = resume_tailor.resume_pins(
        {"resume": {"always_include": [{"entry": "Example Co 0", "bullets": ["100k in funding"]}]}}
    )

    pinned, notes = resume_tailor.pinned_entries(pool, pins)

    assert pins == [Pin("Example Co 0", ["100k in funding"])]
    assert pinned == {"j1": [], "j0": ["100k in funding"]} and not notes


def test_pinned_entries_beat_the_caps_and_keep_their_bullets() -> None:
    pool = _pool(jobs=6, projects=0)
    pinned = {"j5": ["900 interviews", "100k in funding"]}
    draft = _draft([f"j{n}" for n in range(4)])  # the draft left the pinned job out

    sections, _skills, notes, _sources = _keep(draft, pool, pinned)

    jobs = sections[0][1]
    assert [entry.title for entry in jobs] == ["Engineer 5", "Engineer 0", "Engineer 1",
                                               "Engineer 2"]
    assert jobs[0].pinned and jobs[0].bullets == [FACTS[1], FACTS[2]]
    assert jobs[0].locked == [FACTS[1], FACTS[2]]
    assert "Left out Engineer 3: the resume already has 4 jobs." in notes


def test_a_pinned_bullet_the_draft_already_wrote_is_locked() -> None:
    pool = _pool(jobs=1, projects=0)

    sections, *_rest = _keep(_draft(pool), pool, {"j0": ["900 interviews"]})

    assert sections[0][1][0].locked == [OTHER["text"]]


def test_an_unknown_pin_is_noted() -> None:
    _pinned, notes = resume_tailor.pinned_entries(_pool(1, 0), [Pin("Nowhere Inc")])

    assert notes == ["Could not find 'Nowhere Inc', set to always include, in your documents."]


def test_the_prompt_states_the_caps_and_asks_to_fill_the_page() -> None:
    system = resume_tailor.TAILOR_PROMPT.format(
        max_entries=8, headings='"Experience"', max_experiences=4, max_projects=3
    )

    assert "at most 4 jobs" in system and "at most 3\nprojects" in system
    assert "never fewer\nthan 2" in system and "must fill one page" in system


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


class ScriptedClient:
    """Returns each tailor reply in turn and records the requests."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies, self.requests = list(replies), []
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=self.messages)

    def create(self, **request):
        self.requests.append(request)
        payload = self.replies.pop(0)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )


def test_a_draft_that_leaves_the_page_empty_is_sent_back_for_more() -> None:
    pool = _pool(jobs=4, projects=0)
    client = ScriptedClient([_draft(["j0"]), _draft(list(pool))])

    sections, *_rest = resume_tailor._tailor(
        {"company": "Acme", "title": "AI Engineer"}, Keywords(), pool, list(DOCS.values()),
        client=client, model="m", max_entries=8,
    )

    assert "fills only about" in client.requests[-1]["messages"][-1]["content"]
    assert len(sections[0][1]) == 4


def _resume(bullets: list[int], **fields) -> TailoredResume:
    entries = [
        Entry(f"e{n}", f"Engineer {n}", bullets=[f"Built tool {n}.{b}" for b in range(count)])
        for n, count in enumerate(bullets)
    ]
    for entry in entries[:1]:
        for name, value in fields.items():
            setattr(entry, name, value(entry) if callable(value) else value)
    return TailoredResume("Jordan Example", [], [], [("Experience", entries)], [], Keywords())


def test_trimming_drops_a_thin_entry_and_never_leaves_one_bullet() -> None:
    resume = _resume([3, 2, 2])

    assert resume_tailor._trim_one(resume) == "dropped a bullet from Engineer 0"
    assert resume_tailor._trim_one(resume) == "dropped Engineer 2"
    assert resume_tailor._trim_one(resume) == "dropped Engineer 1"
    assert resume_tailor._trim_one(resume) is None
    assert [len(entry.bullets) for entry in resume.sections[0][1]] == [2]


def test_trimming_keeps_pinned_entries_and_locked_bullets() -> None:
    resume = _resume([3, 2], pinned=True, locked=lambda entry: list(entry.bullets))

    assert resume_tailor._trim_one(resume) == "dropped Engineer 1"
    assert resume_tailor._trim_one(resume) is None
    assert len(resume.sections[0][1][0].bullets) == 3
