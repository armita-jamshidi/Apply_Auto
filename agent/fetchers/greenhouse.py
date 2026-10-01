"""Fetcher for Greenhouse's public job board API."""

import logging
import time
from typing import Any

import httpx

from agent.types import JobListing

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
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")

    owns_client = client is None
    http = client or httpx.Client(timeout=20.0)
    url = GREENHOUSE_JOBS_URL.format(board_token=board_token.strip())
    try:
        payload = _request_jobs(http, url, max_retries, backoff_seconds)
    finally:
        if owns_client:
            http.close()

    results: list[JobListing] = []
    for raw_job in payload.get("jobs", []):
        try:
            location = raw_job.get("location") or {}
            results.append(
                JobListing(
                    source="greenhouse",
                    platform="greenhouse",
                    company=company,
                    title=str(raw_job["title"]).strip(),
                    url=str(raw_job["absolute_url"]).strip(),
                    location_raw=str(location.get("name", "")).strip(),
                    description=str(raw_job.get("content") or "").strip(),
                )
            )
        except (AttributeError, KeyError, TypeError) as error:
            LOGGER.warning("Skipping malformed Greenhouse listing for %s: %s", company, error)
    return results


def _request_jobs(
    client: httpx.Client,
    url: str,
    max_retries: int,
    backoff_seconds: float,
) -> dict[str, Any]:
    for attempt in range(max_retries):
        try:
            response = client.get(url, params={"content": "true"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs", []), list):
                raise ValueError("Greenhouse response must contain a jobs list")
            return payload
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            retryable = not isinstance(error, httpx.HTTPStatusError) or (
                error.response.status_code == 429 or error.response.status_code >= 500
            )
            if not retryable or attempt + 1 == max_retries:
                raise
            delay = backoff_seconds * (2**attempt)
            LOGGER.warning("Greenhouse request failed; retrying in %.1fs: %s", delay, error)
            time.sleep(delay)
    raise RuntimeError("Greenhouse retry loop ended unexpectedly")
