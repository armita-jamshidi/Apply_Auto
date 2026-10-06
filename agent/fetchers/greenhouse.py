"""Fetcher for Greenhouse's public job board API."""

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from agent.types import JobListing

from .http import parse_posted, request_json

LOGGER = logging.getLogger(__name__)
GREENHOUSE_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"


def fetch_greenhouse_jobs(
    board_token: str,
    company: str,
    *,
    client: httpx.Client | None = None,
    max_retries: int = 3,
    backoff_seconds: float = 0.5,
) -> list[JobListing]:
    """Fetch public jobs from one Greenhouse board, retrying transient failures."""
    if not board_token.strip():
        raise ValueError("Greenhouse board token cannot be empty")

    owns_client = client is None
    http = client or httpx.Client(timeout=20.0)
    url = GREENHOUSE_JOBS_URL.format(board_token=board_token.strip())
    try:
        payload = request_json(
            http,
            url,
            params={"content": "true"},
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            validate=_as_mapping,
        )
    finally:
        if owns_client:
            http.close()

    results: list[JobListing] = []
    for raw_job in payload.get("jobs", []):
        try:
            location = raw_job.get("location") or {}
            url, apply_url = _job_urls(board_token, raw_job)
            results.append(
                JobListing(
                    source="greenhouse",
                    platform="greenhouse",
                    company=company,
                    title=str(raw_job["title"]).strip(),
                    url=url,
                    apply_url=apply_url,
                    location_raw=str(location.get("name", "")).strip(),
                    description=str(raw_job.get("content") or "").strip(),
                    posted_at=parse_posted(
                        raw_job.get("first_published") or raw_job.get("updated_at")
                    ),
                )
            )
        except (AttributeError, KeyError, TypeError) as error:
            LOGGER.warning("Skipping malformed Greenhouse listing for %s: %s", company, error)
    return results


def _job_urls(board: str, raw_job: dict[str, Any]) -> tuple[str, str | None]:
    """(Greenhouse job URL, the company's own page for the role or None).

    Companies that host their board on their own site publish that page as absolute_url.
    The form filler needs the Greenhouse-hosted page, so that is stored as the job's URL.
    The company page is kept as the place to apply only when it names the role
    ("https://example.com/careers/123"); a list of openings that only carries the id in
    its query ("https://example.com/jobs/search?gh_jid=123") often never opens the role,
    so the Greenhouse page, which always shows it, is used instead.
    """
    absolute = str(raw_job["absolute_url"]).strip()
    host = (urlsplit(absolute).hostname or "").casefold()
    if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
        return absolute, None
    job_id = str(raw_job["id"])
    board_url = f"https://job-boards.greenhouse.io/{board.strip()}/jobs/{job_id}"
    return board_url, absolute if names_role(absolute, job_id) else None


def names_role(url: str, job_id: str) -> bool:
    """Whether a company-hosted Greenhouse link opens the role itself, not a list of jobs.

    It does when the job id is in the path ("/careers/123") or in a fragment route
    ("#/123"); an id only in the query string is often ignored by the page.
    """
    parts = urlsplit(url)
    return any(job_id in re.findall(r"\d+", text) for text in (parts.path, parts.fragment))


def greenhouse_job_id(url: str) -> str | None:
    """The Greenhouse job id in a board link or a company-hosted gh_jid link."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    found = (query.get("gh_jid") or query.get("token") or [""])[0]
    if found.isdigit():
        return found
    match = re.search(r"/jobs/(\d+)", parts.path)
    return match.group(1) if match else None


def _as_mapping(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs", []), list):
        raise ValueError("Greenhouse response must contain a jobs list")
    return payload
