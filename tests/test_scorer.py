"""Mocked tests for structured Anthropic fit scoring."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from agent.scorer import FitAssessment, score_job


def make_client(payload: dict[str, object]) -> Mock:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="submit_fit_assessment", input=payload)]
    )
    client = Mock()
    client.messages.create.return_value = response
    return client


def valid_payload(**updates: object) -> dict[str, object]:
    return {
        "score": 86,
        "reasons": ["Profile lists Python; the role requires Python."],
        "dealbreakers": [],
        "recommended_action": "apply",
        **updates,
    }


def test_score_job_validates_structured_response_and_sends_only_inputs() -> None:
    client = make_client(valid_payload())
    profile = {"skills": ["Python"]}

    result = score_job(
        "Build Python services.",
        profile,
        fit_score_threshold=80,
        model="test-model",
        client=client,
    )

    assert result.recommended_action == "apply"
    assert result.score == 86
    request = client.messages.create.call_args.kwargs
    assert request["model"] == "test-model"
    assert request["tool_choice"] == {"type": "auto"}
    submitted = json.loads(request["messages"][0]["content"])
    assert submitted["candidate_profile"] == profile
    assert submitted["job_description"] == "Build Python services."
    assert submitted["fit_score_threshold"] == 80


def test_score_job_uses_configured_threshold_and_model() -> None:
    client = make_client(valid_payload(score=69))

    result = score_job("Build Python services.", {}, client=client)

    assert result.recommended_action == "skip"
    assert client.messages.create.call_args.kwargs["model"] == "claude-sonnet-5-5"
    assert client.messages.create.call_args.kwargs["model"] == "claude-sonnet-5-5"


def test_score_below_configured_threshold_cannot_recommend_apply() -> None:
    client = make_client(valid_payload(score=69))

    result = score_job("Build Python services.", {}, fit_score_threshold=70, client=client)

    assert result.recommended_action == "skip"


def test_unresolved_dealbreakers_cannot_recommend_apply() -> None:
    client = make_client(
        valid_payload(dealbreakers=["Work authorization is not stated in the profile."])
    )

    result = score_job("Build Python services.", {}, client=client)

    assert result.recommended_action == "manual"


def test_non_nc_hybrid_requirement_is_added_as_dealbreaker_and_skipped() -> None:
    client = make_client(valid_payload())

    result = score_job(
        "Hybrid in San Francisco, CA three days per week.",
        {},
        client=client,
    )

    assert result.recommended_action == "skip"
    assert result.dealbreakers == [
        "Requires hybrid or in-office work outside North Carolina: CA"
    ]


def test_non_nc_work_location_mentions_are_not_flagged_without_work_requirement() -> None:
    client = make_client(valid_payload(recommended_action="manual"))

    result = score_job(
        "Remote role; occasional collaboration with the San Francisco office.",
        {},
        client=client,
    )

    assert result.recommended_action == "manual"
    assert result.dealbreakers == []


@pytest.mark.parametrize("score", [-1, 101])
def test_out_of_range_score_is_rejected(score: int) -> None:
    with pytest.raises(ValidationError):
        FitAssessment.model_validate(valid_payload(score=score))


def test_missing_structured_tool_result_is_rejected() -> None:
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(content=[])

    with pytest.raises(ValueError, match="structured fit assessment"):
        score_job("Build Python services.", {}, client=client)


@pytest.mark.parametrize("threshold", [-1, 101])
def test_invalid_threshold_is_rejected_before_call(threshold: int) -> None:
    client = make_client(valid_payload())

    with pytest.raises(ValueError, match="fit_score_threshold"):
        score_job("Build Python services.", {}, fit_score_threshold=threshold, client=client)
    client.messages.create.assert_not_called()
