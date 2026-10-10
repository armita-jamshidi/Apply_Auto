"""The qualification map: each posting line, the resume bullets that hit it, and their sources."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import apply_kit, dashboard, resume_tailor
from agent.answers import AnswerDecision
from agent.resume_tailor import (
    KEYWORD_PROMPT,
    POOL_PROMPT,
    SUGGEST_PROMPT,
    extract_keywords,
    qualification_map,
    qualification_map_markdown,
    tailor_resume,
)
from db.models import Base, Job

RESUME = """Jordan Example
jordan@example.com
Example State University, B.S. Computer Science, Dec 2025
Research Assistant, Example Lab
Evaluated LLM agents in Python on 40 tasks with an evaluation harness."""
BANK = """# Projects
## Ticket Sorter
Wrote an agent that sorts support tickets with an LLM and a rules layer."""
DESCRIPTION = (
    "<h3>What you'll do</h3><ul><li>Build and evaluate agent workflows for customers.</li></ul>"
    "<h3>Requirements</h3><ul><li>Experience with Python and LLM APIs.</li>"
    "<li>Comfort with Kubernetes in production.</li></ul>"
    "<h3>Nice to have</h3><ul><li>Experience with RAG | vector databases.</li></ul>"
)
JOB = {"company": "Acme", "title": "AI Engineer", "description": DESCRIPTION}
QUALIFICATIONS = [
    {"kind": "required", "text": "Experience with Python and LLM APIs.",
     "terms": ["Python", "LLM APIs", "Go"]},  # Go is not in the line
    {"kind": "required", "text": "Comfort with Kubernetes in production",
     "terms": ["Kubernetes"]},
    {"kind": "required", "text": "Five years of Rust", "terms": ["Rust"]},  # not in the posting
    {"kind": "preferred", "text": "experience with RAG | vector databases",
     "terms": ["RAG", "vector databases"]},
    {"kind": "duty", "text": "Build and evaluate agent workflows for customers.",
     "terms": ["evaluate", "agent workflows"]},
]
EVALUATED = "Evaluated LLM agents in Python on 40 tasks with an evaluation harness."
SORTER = "Wrote an agent that sorts support tickets with an LLM and a rules layer."


@pytest.fixture(autouse=True)
def _estimated_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resume_tailor, "_page_count", lambda _path: None)


class FakeClient:
    """Keywords with qualification lines, a two-entry pool, then one tailored draft."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        if request["system"] == KEYWORD_PROMPT:
            payload = {
                "action_verbs": ["evaluate"], "technologies": ["Python"], "concepts": [],
                "required": ["Python"], "preferred": [], "qualifications": QUALIFICATIONS,
            }
        elif request["system"] == POOL_PROMPT:
            blank = {"location": "", "dates": ""}
            payload = {
                "name": "Jordan Example", "contact": ["jordan@example.com"], "education": [],
                "entries": [
                    {**blank, "title": "Research Assistant", "organization": "Example Lab",
                     "kind": "experience", "doc_id": "resume", "facts": [EVALUATED]},
                    {**blank, "title": "Ticket Sorter", "organization": "",
                     "kind": "project", "doc_id": "bank.md", "facts": [SORTER]},
                ],
            }
        elif request["system"] == SUGGEST_PROMPT:
            payload = {"suggestions": [{
                "keyword": "Kubernetes", "entry_id": "e2",
                "text": "Deployed the ticket sorter on Kubernetes", "basis": SORTER,
            }]}
        else:
            payload = {
                "sections": [{"heading": "Experience", "entries": [
                    {"entry_id": "e1", "bullets": [{
                        "text": "Evaluated LLM agents in Python on 40 tasks",
                        "evidence": [{"doc_id": "resume",
                                      "quote": "Evaluated LLM agents in Python on 40 tasks"}],
                    }]},
                    {"entry_id": "e2", "bullets": [{
                        "text": "Wrote an LLM agent that sorts support tickets",
                        "evidence": [{"doc_id": "bank.md", "quote": SORTER}],
                    }]},
                ]}],
                "skills": [{"label": "Tools", "items": ["Python"]}],
            }
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )


def _library(tmp_path: Path) -> Path:
    library = tmp_path / "library"
    library.mkdir()
    (library / "bank.md").write_text(BANK, encoding="utf-8")
    return library


def test_qualification_lines_are_kept_only_word_for_word_from_the_posting() -> None:
    text = " ".join(resume_tailor._plain_text(DESCRIPTION).split())
    keywords = extract_keywords(text, client=FakeClient(), model="m")

    assert [(q.qualification_id, q.kind, q.text, q.terms) for q in keywords.qualifications] == [
        ("R1", "required", "Experience with Python and LLM APIs", ["Python", "LLM APIs"]),
        ("R2", "required", "Comfort with Kubernetes in production", ["Kubernetes"]),
        # "Five years of Rust" is not in the posting, so it is dropped
        ("P1", "preferred", "Experience with RAG | vector databases", ["RAG", "vector databases"]),
        ("D1", "duty", "Build and evaluate agent workflows for customers", [
            "evaluate", "agent workflows"]),
    ]
    # Their terms are keywords the resume is built to hit.
    assert keywords.required == ["Python", "LLM APIs", "Kubernetes"]
    assert keywords.preferred == ["RAG", "vector databases"]


def test_each_line_maps_to_the_bullets_that_hit_it_and_their_sources(tmp_path: Path) -> None:
    client = FakeClient()
    resume = tailor_resume(
        JOB, RESUME, client=client, model="m", library_dir=_library(tmp_path), ask=None
    )

    tailor_request = json.loads(client.requests[-1]["messages"][0]["content"])
    assert [q["id"] for q in tailor_request["qualifications"]] == ["R1", "R2", "P1", "D1"]
    rows = {row["id"]: row for row in qualification_map(resume)}

    (python,) = rows["R1"]["matches"]
    assert python["bullet"] == "Evaluated LLM agents in Python on 40 tasks"
    assert python["entry"] == "Research Assistant, Example Lab"
    assert python["terms"] == ["Python"]
    assert python["sources"] == [
        {"document": "Resume", "quote": "Evaluated LLM agents in Python on 40 tasks"}
    ]
    assert rows["R1"]["missing"] == ["LLM APIs"]
    # Kubernetes is in no document: a gap, with the suggestion offered but kept off the resume.
    assert rows["R2"]["matches"] == []
    assert rows["R2"]["suggestions"][0]["text"] == "Deployed the ticket sorter on Kubernetes"
    assert "Kubernetes" not in resume.text()
    # A term counts only spelled as the posting spells it: "Evaluated" does not hit "evaluate".
    assert rows["D1"]["matches"] == []
    assert rows["D1"]["missing"] == ["evaluate", "agent workflows"]


def test_a_bullet_trimmed_from_the_resume_no_longer_counts(tmp_path: Path) -> None:
    resume = tailor_resume(
        JOB, RESUME, client=FakeClient(), model="m", library_dir=_library(tmp_path), ask=None
    )
    for _heading, entries in resume.sections:
        for entry in entries:
            entry.bullets = [b for b in entry.bullets if "Python" not in b]

    rows = {row["id"]: row for row in qualification_map(resume)}

    assert rows["R1"]["matches"] == []
    assert rows["R1"]["in_skills"] == ["Python"]  # still listed under Skills


def test_the_markdown_table_lists_every_line_and_escapes_pipes(tmp_path: Path) -> None:
    resume = tailor_resume(
        JOB, RESUME, client=FakeClient(), model="m", library_dir=_library(tmp_path), ask=None
    )

    text = qualification_map_markdown(qualification_map(resume), company="Acme", title="AI Eng")

    assert "| # | Type | From the posting | Your source | Bullet on this resume |" in text
    assert "| R1 | Required | Experience with Python and LLM APIs |" in text
    assert "Experience with RAG \\| vector databases" in text
    assert "Not on this resume; suggested: Deployed the ticket sorter on Kubernetes" in text
    assert "missing: LLM APIs" in text
    assert "(required: 1 of 2)" in text


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def test_the_map_is_saved_with_the_kit_and_shown_in_the_apply_panel(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apply_kit, "KITS_DIR", tmp_path / "kits")
    monkeypatch.setattr(dashboard, "KITS_DIR", tmp_path / "kits")
    monkeypatch.setattr("agent.resume_tailor.LIBRARY_DIR", _library(tmp_path))
    job = Job(
        source="lever", platform="lever", company="Acme", title="AI Engineer",
        url="https://jobs.lever.co/acme/1", location_raw="Remote - US",
        location_category="remote_us", description=DESCRIPTION, status="new",
    )
    session.add(job)
    session.flush()
    session.refresh(job)

    kit = apply_kit.build_kit(
        session, job, profile={}, resume_text=RESUME, client=FakeClient(),
        answer_model="m", resume_model="m", ask=None,
        answerer=lambda question, _long: AnswerDecision(None, None, True, "No source."),
    )

    assert not kit.problems
    assert kit.map_path.read_text(encoding="utf-8").startswith("# Qualification map: AI Engineer")
    session.refresh(job)
    stored = apply_kit.latest_kit(job).field_notes[apply_kit.QUALIFICATION_MAP_NOTE]
    assert [row["id"] for row in json.loads(stored)] == ["R1", "R2", "P1", "D1"]
    page = tmp_path / "dashboard.html"
    dashboard.write_dashboard(session, page, fit_threshold=70)
    html = page.read_text(encoding="utf-8")
    assert "How your resume hits each qualification" in html
    assert "On your resume (Research Assistant, Example Lab): Evaluated LLM agents" in html
    assert "Suggested, if true, for Ticket Sorter: Deployed the ticket sorter" in html
    # The map is part of the kit, not a question on the form.
    assert ">Qualification map<" not in html


def test_a_source_naming_a_tool_another_way_supports_the_postings_spelling() -> None:
    keywords = resume_tailor.Keywords(technologies=["PostgreSQL", "Kubernetes"])
    cv = resume_tailor.LibraryDocument("cv", "Master CV", "Stored notes in Postgres.")
    documents = {"cv": cv}

    renamed = {"text": "Stored notes in PostgreSQL",
               "evidence": [{"doc_id": "cv", "quote": "Stored notes in Postgres."}]}
    invented = {"text": "Stored notes in PostgreSQL on Kubernetes",
                "evidence": [{"doc_id": "cv", "quote": "Stored notes in Postgres."}]}

    assert resume_tailor.bullet_problem(renamed, keywords, documents) is None
    assert "Kubernetes" in resume_tailor.bullet_problem(invented, keywords, documents)
    assert resume_tailor._mentioned("PostgreSQL", documents.values())
