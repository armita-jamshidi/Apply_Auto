"""Tests for source-quoted custom application answers."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.answers import WHY_TOOL_NAME, answer_custom_question


def make_client(
    answer: str | None,
    evidence: str | None,
    *,
    tool_name: str = "submit_grounded_answer",
) -> Mock:
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name=tool_name,
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
        "What is your most relevant project?",
        {"projects": [{"name": "Evidence Tool", "summary": project_summary}]},
        "",
        client=make_client(project_summary, project_summary),
        model="test-model",
    )

    assert decision.answer == project_summary
    assert decision.needs_manual_review is False


@pytest.mark.parametrize(
    "question",
    [
        "Please provide a summary highlighting your top two exceptional academic and/or "
        "professional accomplishments. Ideally, the examples you share will be a reflection of "
        "your most highly technical accomplishments.",
        "Describe a relevant project.",
        "Tell us about a technical challenge you solved.",
        "What is your proudest achievement?",
    ],
)
def test_open_ended_questions_get_a_cited_draft(question: str) -> None:
    statement = "I built a Python service for literature analysis."
    client = make_client(None, None, tool_name=WHY_TOOL_NAME)
    client.messages.create.return_value.content[0].input = {
        "answer": statement,
        "claims": [
            {
                "statement": statement,
                "evidence": "Built a Python service for literature analysis.",
                "source": "resume",
            }
        ],
    }

    decision = answer_custom_question(
        question,
        {"name": "Sample"},
        "Built a Python service for literature analysis.",
        client=client,
        model="test-model",
    )

    assert decision.answer == statement
    assert decision.is_motivation_draft is True
    assert decision.needs_manual_review is True
    assert client.messages.create.call_args.kwargs["tools"][0]["name"] == WHY_TOOL_NAME


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Have you ever applied to Anthropic before?", "No"),
        ("Have you interviewed at Anthropic previously?", "No"),
        ("AI Policy for Application: confirm your understanding.", "Yes"),
        ("Have you read the AI usage guidelines and do you agree?", "Yes"),
    ],
)
def test_user_directed_yes_no_answers_do_not_call_anthropic(
    question: str,
    expected: str,
) -> None:
    client = Mock()

    decision = answer_custom_question(question, {}, "", client=client)

    assert decision.answer == expected
    assert decision.needs_manual_review is False
    client.messages.create.assert_not_called()


def test_qualification_question_is_manual_not_unconditional_yes() -> None:
    client = Mock()

    decision = answer_custom_question(
        "Do you meet the requirements listed above?",
        {"skills": ["Python"]},
        "Python developer.",
        client=client,
    )

    assert decision.answer is None
    assert decision.needs_manual_review is True
    assert "unconditional yes" in (decision.reason or "")
    client.messages.create.assert_not_called()


def test_motivation_question_returns_cited_draft_for_review_only() -> None:
    answer = (
        "This AI research program interests me. "
        "I built a Python service for literature analysis."
    )
    client = make_client(
        answer,
        None,
        tool_name=WHY_TOOL_NAME,
    )
    client.messages.create.return_value.content[0].input = {
        "answer": answer,
        "claims": [
            {
                "statement": "This AI research program interests me.",
                "evidence": "AI research program.",
                "source": "job_description",
            },
            {
                "statement": "I built a Python service for literature analysis.",
                "evidence": "Built a Python service for literature analysis.",
                "source": "profile",
            },
        ],
    }

    decision = answer_custom_question(
        "Why do you want to participate in this program?",
        {"projects": [{"summary": "Built a Python service for literature analysis."}]},
        "",
        client=client,
        model="test-model",
        job_context={
            "company": "Example Lab",
            "title": "Research Fellow",
            "description": "AI research program.",
        },
    )

    assert decision.answer == answer
    assert decision.needs_manual_review is True
    assert "candidate review" in (decision.reason or "")
    request_context = json.loads(client.messages.create.call_args.kwargs["messages"][0]["content"])
    assert request_context["job_context"]["company"] == "Example Lab"
    assert "[job_description] AI research program." in (decision.evidence or "")


def test_motivation_draft_with_unverifiable_claim_is_rejected() -> None:
    client = make_client(None, None, tool_name=WHY_TOOL_NAME)
    client.messages.create.return_value.content[0].input = {
        "answer": "I led a team of 20 engineers.",
        "claims": [
            {
                "statement": "I led a team of 20 engineers.",
                "evidence": "Led a team of 20 engineers.",
                "source": "profile",
            }
        ],
    }

    decision = answer_custom_question(
        "Why are you interested in this role?",
        {"experience": ["Built backend systems."]},
        "Built backend systems.",
        client=client,
        model="test-model",
    )

    assert decision.answer is None
    assert decision.needs_manual_review is True
    assert "could not be verified" in (decision.reason or "")
