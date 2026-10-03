"""Mocked tests for Lever, Ashby, and SmartRecruiters public posting APIs."""

from unittest.mock import patch

import httpx

from agent.fetchers.ashby import fetch_ashby_jobs
from agent.fetchers.lever import fetch_lever_jobs
from agent.fetchers.smartrecruiters import fetch_smartrecruiters_jobs


def test_fetch_lever_jobs_normalizes_postings_and_skips_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["mode"] == "json"
        return httpx.Response(
            200,
            json=[
                {
                    "text": "Software Engineer",
                    "hostedUrl": "https://jobs.lever.co/example/1",
                    "categories": {"location": "Remote - US"},
                    "descriptionPlain": "Build services.",
                },
                {"text": "Malformed"},
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_lever_jobs("example", "Example Co", client=client)
    client.close()

    assert len(jobs) == 1
    assert jobs[0].platform == "lever"
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].location_raw == "Remote - US"
    assert jobs[0].description == "Build services."


def test_fetch_lever_jobs_includes_requirement_lists_and_closing_text() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "text": "Forward Deployed Engineer",
                    "hostedUrl": "https://jobs.lever.co/example/2",
                    "categories": {"location": "Raleigh, NC"},
                    "descriptionPlain": "Intro paragraph.",
                    "lists": [
                        {
                            "text": "What We Require",
                            "content": "<li>Active security clearance &amp; US citizenship</li>"
                            "<li><b>Python</b> experience</li>",
                        },
                        {"text": "Empty", "content": ""},
                    ],
                    "additionalPlain": "Salary range: listed.",
                }
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_lever_jobs("example", "Example Co", client=client)
    client.close()

    assert jobs[0].description == (
        "Intro paragraph.\n\n"
        "What We Require\n- Active security clearance & US citizenship\n- Python experience\n\n"
        "Empty\n\n"
        "Salary range: listed."
    )


def test_fetch_ashby_jobs_uses_public_board_and_skips_unlisted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/posting-api/job-board/example")
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "title": "ML Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/example/1",
                        "location": "Durham, NC",
                        "descriptionPlain": "Build models.",
                        "isListed": True,
                    },
                    {"title": "Hidden", "isListed": False},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_ashby_jobs("example", "Example Co", client=client)
    client.close()

    assert len(jobs) == 1
    assert jobs[0].platform == "ashby"
    assert jobs[0].location_raw == "Durham, NC"


def test_fetch_smartrecruiters_paginates_and_builds_listing() -> None:
    requests: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/postings/job-" in request.url.path:
            return httpx.Response(
                200,
                json={"jobAd": {"sections": {"jobDescription": {"text": "Role details."}}}},
            )
        limit = int(request.url.params["limit"])
        offset = int(request.url.params["offset"])
        requests.append((limit, offset))
        if offset == 0:
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "id": "job-1",
                            "name": "Backend Engineer",
                            "location": {"city": "Raleigh", "region": "NC", "country": "US"},
                        }
                        for _ in range(limit)
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [
                    {"id": "job-101", "name": "AI Engineer", "location": {"country": "US"}}
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_smartrecruiters_jobs("example", "Example Co", client=client)
    client.close()

    assert len(jobs) == 101
    assert requests == [(100, 0), (100, 100)]
    assert jobs[0].location_raw == "Raleigh, NC, US"
    assert jobs[-1].url == "https://jobs.smartrecruiters.com/example/job-101"
    assert jobs[-1].description == "Role details."


def test_fetchers_retry_transient_http_errors() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch("agent.fetchers.http.time.sleep"):
        jobs = fetch_lever_jobs("example", "Example Co", client=client, max_retries=2)
    client.close()

    assert jobs == []
    assert calls == 2


def test_smartrecruiters_include_skips_description_requests() -> None:
    detail_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/postings"):
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"id": "1", "name": "Graduate Engineer", "location": {"country": "us"}},
                        {"id": "2", "name": "Senior Engineer", "location": {"country": "us"}},
                    ]
                },
            )
        detail_requests.append(request.url.path)
        return httpx.Response(200, json={"jobAd": {"sections": {"a": {"text": "Build."}}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_smartrecruiters_jobs(
        "Example", "Example Co", client=client, include=lambda job: "Senior" not in job.title
    )
    client.close()

    assert [job.title for job in jobs] == ["Graduate Engineer"]
    assert jobs[0].description == "Build."
    assert detail_requests == ["/v1/companies/Example/postings/1"]
