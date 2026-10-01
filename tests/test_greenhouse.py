"""Tests for the Greenhouse API fetcher with mocked HTTP responses."""

from unittest.mock import patch

import httpx

from agent.fetchers.greenhouse import fetch_greenhouse_jobs


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
    with patch("agent.fetchers.greenhouse.time.sleep"):
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
