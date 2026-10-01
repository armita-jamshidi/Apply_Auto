"""Tests for source-quoted custom application answers."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from agent.answers import answer_custom_question


def make_client(answer: str | None, evidence: str | None) -> Mock:
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="submit_grounded_answer",
                input={"answer": answer, "evidence": evidence},
            )
        ]
    )
    client = Mock()
    client.messages.create.return_value = response
    return client


def test_answer_is_accepted_when_exact_source_quote_is_verified() -> None:
    profile = {
        "personal": {"github": "https://github.com/example"},
        "skills": ["Python", "FastAPI"],
        "projects": [
            {
                "name": "Example Project",
                "summary": "Built a Python service.",
                "skills": ["Python"],
            }
        ],
    }
    client = make_client("Python", "Python")
    decision = answer_custom_question(
        "Which programming language do you use?",
        profile,
        "Built internal data services.",
        client=client,
        model="test-model",
    )

    assert decision.answer == "Python"
    assert decision.evidence == "Python"
    assert decision.needs_manual_review is False
    sent_context = json.loads(client.messages.create.call_args.kwargs["messages"][0]["content"])
    assert sent_context["candidate_profile"]["personal"]["github"] == (
        "https://github.com/example"
    )
    assert sent_context["candidate_profile"]["projects"] == profile["projects"]


def test_answer_with_fabricated_evidence_is_sent_to_manual_review() -> None:
    decision = answer_custom_question(
        "How many years of experience do you have?",
        {"skills": ["Python"]},
        "Built internal data services.",
        client=make_client("Eight years", "Eight years"),
        model="test-model",
    )

    assert decision.answer is None
    assert decision.needs_manual_review is True
    assert "could not be verified" in (decision.reason or "")


def test_paraphrased_answer_is_rejected_even_with_valid_quote() -> None:
    decision = answer_custom_question(
        "Summarize your background.",
        {"experience": ["Built internal data services."]},
        "",
        client=make_client("I built internal data services.", "Built internal data services."),
        model="test-model",
    )

    assert decision.needs_manual_review is True
    assert decision.answer is None


def test_no_available_sources_skips_anthropic_call() -> None:
    client = make_client(None, None)

    decision = answer_custom_question("Why this company?", {}, "", client=client)

    assert decision.needs_manual_review is True
    assert "No profile or resume facts" in (decision.reason or "")
    client.messages.create.assert_not_called()


def test_project_summary_can_ground_a_custom_answer() -> None:
    project_summary = "Built a Python service for literature analysis."
    decision = answer_custom_question(
        "Describe a relevant project.",
        {"projects": [{"name": "Evidence Tool", "summary": project_summary}]},
        "",
        client=make_client(project_summary, project_summary),
        model="test-model",
    )

    assert decision.answer == project_summary
    assert decision.needs_manual_review is False
