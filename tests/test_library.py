"""Tests for the source library and the agent that decides what to look up in it."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent.answers import answer_custom_question
from agent.library import has_extra_sources, load_library, outline, read, search
from agent.library_agent import draft_from_library

ESSAY = """# Evidence Tool write-up

I built Evidence Tool, a Python service that extracts findings from medical papers.

It cut literature review time from two weeks to two days for a team of five researchers.
"""


def library(tmp_path: Path) -> Path:
    folder = tmp_path / "library"
    (folder / "projects").mkdir(parents=True)
    (folder / "projects" / "evidence.md").write_text(ESSAY, encoding="utf-8")
    (folder / "why-ai.txt").write_text("I care about safe AI agents.", encoding="utf-8")
    (folder / "empty.md").write_text("   ", encoding="utf-8")
    return folder


def test_library_loads_files_with_resume_profile_and_job(tmp_path: Path) -> None:
    documents = load_library(
        library(tmp_path),
        resume_text="Python engineer.",
        profile_facts=["Cary, NC"],
        job_description="Build agents.",
    )

    assert [doc.doc_id for doc in documents] == [
        "resume",
        "profile",
        "job_description",
        "projects/evidence.md",
        "why-ai.txt",
    ]
    assert documents[3].title == "Evidence Tool write-up"
    assert has_extra_sources(documents)
    assert not has_extra_sources(documents[:3])
    assert outline(documents)[3]["characters"] == len(ESSAY.strip())


def test_search_ranks_matching_passages_and_read_pages_through(tmp_path: Path) -> None:
    documents = load_library(library(tmp_path), resume_text="Java developer.")

    hits = search(documents, "literature review time")
    assert hits[0]["doc_id"] == "projects/evidence.md"
    assert "two weeks to two days" in hits[0]["passage"]
    assert search(documents, "the and of") == []

    first = read(documents, "projects/evidence.md", 0)
    assert first["next_offset"] is None and first["text"].startswith("# Evidence Tool")
    assert "error" in read(documents, "missing.md", 0)


def tool_call(name: str, arguments: dict, call_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=arguments, id=call_id)


def scripted_client(*turns: list[SimpleNamespace]) -> Mock:
    client = Mock()
    client.messages.create.side_effect = [
        SimpleNamespace(stop_reason="tool_use", content=blocks) for blocks in turns
    ]
    return client


def tool_results(client: Mock) -> list[dict]:
    """Every tool result sent back to the model, in order (the history list is shared)."""
    messages = client.messages.create.call_args.kwargs["messages"]
    return [
        block
        for message in messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
    ]


def submit(statement: str, evidence: str, doc_id: str, call_id: str) -> SimpleNamespace:
    return tool_call(
        "submit_draft",
        {
            "answer": statement,
            "claims": [{"statement": statement, "evidence": evidence, "doc_id": doc_id}],
        },
        call_id,
    )


def test_agent_looks_things_up_then_submits_a_verified_draft(tmp_path: Path) -> None:
    documents = load_library(library(tmp_path), resume_text="Python engineer.")
    statement = "I cut literature review time from two weeks to two days."
    client = scripted_client(
        [tool_call("search_library", {"query": "review time"}, "t1")],
        [tool_call("read_document", {"doc_id": "projects/evidence.md", "offset": 0}, "t2")],
        [
            submit(
                statement,
                "cut literature review time from two weeks to two days",
                "projects/evidence.md",
                "t3",
            )
        ],
    )

    draft = draft_from_library(
        "Describe your proudest achievement.",
        documents,
        {"company": "Acme"},
        model="test-model",
        client=client,
    )

    assert draft.answer == statement
    assert draft.documents_read == ["projects/evidence.md"]
    search_result = json.loads(tool_results(client)[0]["content"])
    assert search_result["passages"][0]["doc_id"] == "projects/evidence.md"


def test_agent_gets_one_chance_to_fix_an_unverifiable_quote(tmp_path: Path) -> None:
    documents = load_library(library(tmp_path), resume_text="Python engineer.")
    good = "I built a Python service that extracts findings from medical papers."
    client = scripted_client(
        [
            submit(
                "I cut review time by 90%.", "cut review time by 90%", "projects/evidence.md", "t1"
            )
        ],
        [
            submit(
                good,
                "a Python service that extracts findings from medical papers",
                "projects/evidence.md",
                "t2",
            )
        ],
    )

    draft = draft_from_library("Tell us about a project.", documents, {}, model="m", client=client)

    assert draft.answer == good
    rejection = tool_results(client)[0]
    assert rejection["is_error"] is True and "word for word" in rejection["content"]

    stubborn = scripted_client(
        [submit("I won awards.", "won awards", "resume", "t1")],
        [submit("I won awards.", "won awards", "resume", "t2")],
    )
    failed = draft_from_library(
        "Tell us about a project.", documents, {}, model="m", client=stubborn
    )
    assert failed.answer is None and "word for word" in failed.reason


def test_written_questions_use_the_library_agent_when_it_has_extra_sources(
    tmp_path: Path,
) -> None:
    statement = (
        "I built Evidence Tool, a Python service that extracts findings from medical papers."
    )
    client = scripted_client(
        [submit(statement, "I built Evidence Tool, a Python service", "projects/evidence.md", "t1")]
    )

    decision = answer_custom_question(
        "What is the most impactful thing you've built? What was your role?",
        {"name": "Sample"},
        "Python engineer.",
        client=client,
        model="test-model",
        long_form=True,
        library_dir=library(tmp_path),
    )

    assert decision.answer == statement
    assert decision.is_motivation_draft and decision.needs_manual_review
    assert "[projects/evidence.md]" in decision.evidence
    tools = {tool["name"] for tool in client.messages.create.call_args.kwargs["tools"]}
    assert {"search_library", "read_document", "submit_draft"} <= tools
