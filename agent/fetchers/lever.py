"""Fetcher for Lever's public postings API."""

import logging
import re
from html import unescape
from typing import Any

import httpx

from agent.types import JobListing

from .http import request_json

LOGGER = logging.getLogger(__name__)
LEVER_POSTINGS_URL = "https://api.lever.co/v0/postings/{site}"


def fetch_lever_jobs(
    site: str,
    company: str,
    *,
    client: httpx.Client | None = None,
    max_retries: int = 3,
    backoff_seconds: float = 0.5,
) -> list[JobListing]:
    """Fetch job postings from a Lever site name, skipping malformed items."""
    if not site.strip():
        raise ValueError("Lever site name cannot be empty")
    owns_client = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        payload = request_json(
            http,
            LEVER_POSTINGS_URL.format(site=site.strip()),
            params={"mode": "json"},
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            validate=_as_list,
        )
    finally:
        if owns_client:
            http.close()

    jobs: list[JobListing] = []
    for item in payload:
        try:
            categories = item.get("categories") or {}
            location = categories.get("location") or categories.get("allLocations") or ""
            description = _full_description(item)
            jobs.append(
                JobListing(
                    source="lever",
                    platform="lever",
                    company=company,
                    title=str(item["text"]).strip(),
                    url=str(item.get("hostedUrl") or item["applyUrl"]).strip(),
                    location_raw=str(location).strip(),
                    description=str(description).strip(),
                )
            )
        except (AttributeError, KeyError, TypeError) as error:
            LOGGER.warning("Skipping malformed Lever listing for %s: %s", company, error)
    return jobs


def _full_description(item: dict[str, Any]) -> str:
    """Join the intro, requirement lists, and closing text; descriptionPlain is only the intro."""
    parts = [str(item.get("descriptionPlain") or item.get("description") or "").strip()]
    for section in item.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("text") or "").strip()
        bullets = [
            f"- {_html_to_text(entry)}"
            for entry in re.findall(r"<li[^>]*>(.*?)</li>", str(section.get("content") or ""), re.S)
            if _html_to_text(entry)
        ]
        if heading or bullets:
            parts.append("\n".join(([heading] if heading else []) + bullets))
    parts.append(str(item.get("additionalPlain") or "").strip())
    return "\n\n".join(part for part in parts if part)


def _html_to_text(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _as_list(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ValueError("Lever response must be a list of job objects")
    return payload
