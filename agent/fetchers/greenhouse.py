"""Fetcher for Greenhouse's public job board API."""

import logging
from typing import Any

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
            results.append(
                JobListing(
                    source="greenhouse",
                    platform="greenhouse",
                    company=company,
                    title=str(raw_job["title"]).strip(),
                    url=str(raw_job["absolute_url"]).strip(),
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


def _as_mapping(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs", []), list):
        raise ValueError("Greenhouse response must contain a jobs list")
    return payload
