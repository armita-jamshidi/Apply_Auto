"""Tests for the Greenhouse API fetcher with mocked HTTP responses."""

from unittest.mock import patch

import httpx

from agent.fetchers.greenhouse import (
    fetch_greenhouse_jobs,
    fetch_greenhouse_questions,
    greenhouse_board_token,
)


def test_fetch_greenhouse_jobs_parses_public_board_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["content"] == "true"
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://boards.greenhouse.io/example/jobs/1",
                        "location": {"name": "Remote - US"},
                        "content": "<p>Build services.</p>",
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_greenhouse_jobs("example", "Example Co", client=client)
    client.close()

    assert len(jobs) == 1
    assert jobs[0].company == "Example Co"
    assert jobs[0].location_raw == "Remote - US"
    assert jobs[0].description == "<p>Build services.</p>"


def test_fetch_greenhouse_jobs_retries_transient_server_error() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"jobs": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch("agent.fetchers.http.time.sleep"):
        jobs = fetch_greenhouse_jobs("example", "Example Co", client=client, max_retries=2)
    client.close()

    assert jobs == []
    assert calls == 2


def test_malformed_listing_is_skipped_without_losing_valid_jobs() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {"title": "Incomplete listing"},
                    {
                        "title": "Valid role",
                        "absolute_url": "https://boards.greenhouse.io/example/jobs/2",
                        "location": {"name": "Raleigh, NC"},
                    },
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_greenhouse_jobs("example", "Example Co", client=client)
    client.close()

    assert [job.title for job in jobs] == ["Valid role"]


def test_fetch_greenhouse_questions_lists_answerable_questions_in_form_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/boards/example/jobs/42"
        assert request.url.params["questions"] == "true"
        return httpx.Response(
            200,
            json={
                "questions": [
                    {"label": "First Name", "fields": [{"type": "input_text"}]},
                    {"label": "Resume/CV", "fields": [{"type": "input_file"},
                                                      {"type": "textarea"}]},
                    {"label": "Cover Letter", "fields": [{"type": "input_file"}]},
                    {"label": "Why Example?", "fields": [{"type": "textarea"}]},
                    {"label": "Need sponsorship?",
                     "fields": [{"type": "multi_value_single_select"}]},
                ],
                "location_questions": [
                    {"label": "Location (City)", "fields": [{"type": "input_text"}]},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    questions = fetch_greenhouse_questions("example", "42", client=client)
    client.close()

    assert questions == [
        "First Name", "Resume/CV", "Why Example?", "Need sponsorship?", "Location (City)",
    ]


def test_greenhouse_board_token_reads_hosted_links_only() -> None:
    assert greenhouse_board_token("https://job-boards.greenhouse.io/example/jobs/42") == "example"
    assert greenhouse_board_token("https://boards.greenhouse.io/example/jobs/42?gh_src=x") == (
        "example"
    )
    assert greenhouse_board_token(
        "https://boards.greenhouse.io/embed/job_app?for=example&token=42"
    ) == "example"
    assert greenhouse_board_token("https://example.com/careers/42") is None
