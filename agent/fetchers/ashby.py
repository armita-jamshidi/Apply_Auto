"""Fetcher for Ashby's public job board API."""

import logging
from typing import Any

import httpx

from agent.types import JobListing

from .http import request_json

LOGGER = logging.getLogger(__name__)
ASHBY_JOBS_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"


def fetch_ashby_jobs(
    board: str,
    company: str,
    *,
    client: httpx.Client | None = None,
    max_retries: int = 3,
    backoff_seconds: float = 0.5,
) -> list[JobListing]:
    """Fetch public Ashby board postings, skipping malformed items."""
    if not board.strip():
        raise ValueError("Ashby board name cannot be empty")
    owns_client = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        payload = request_json(
            http,
            ASHBY_JOBS_URL.format(board=board.strip()),
            params={"includeCompensation": "false"},
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            validate=_as_mapping,
        )
    finally:
        if owns_client:
            http.close()

    jobs: list[JobListing] = []
    for item in payload.get("jobs", []):
        try:
            if item.get("isListed") is False:
                continue
            location = item.get("location") or item.get("locationName") or ""
            description = item.get("descriptionPlain") or item.get("descriptionHtml") or ""
            jobs.append(
                JobListing(
                    source="ashby",
                    platform="ashby",
                    company=company,
                    title=str(item["title"]).strip(),
                    url=str(item["jobUrl"]).strip(),
                    location_raw=str(location).strip(),
                    description=str(description).strip(),
                )
            )
        except (AttributeError, KeyError, TypeError) as error:
            LOGGER.warning("Skipping malformed Ashby listing for %s: %s", company, error)
    return jobs


def _as_mapping(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs", []), list):
        raise ValueError("Ashby response must contain a jobs list")
    return payload
