"""Fetcher for SmartRecruiters' public postings API."""

import logging
from typing import Any
from urllib.parse import quote

import httpx

from agent.types import JobListing

from .http import request_json

LOGGER = logging.getLogger(__name__)
SMARTRECRUITERS_POSTINGS_URL = "https://api.smartrecruiters.com/v1/companies/{company_id}/postings"


def fetch_smartrecruiters_jobs(
    company_id: str,
    company: str,
    *,
    client: httpx.Client | None = None,
    max_retries: int = 3,
    backoff_seconds: float = 0.5,
) -> list[JobListing]:
    """Fetch public SmartRecruiters postings, following API pagination."""
    if not company_id.strip():
        raise ValueError("SmartRecruiters company id cannot be empty")
    owns_client = client is None
    http = client or httpx.Client(timeout=20.0)
    jobs: list[JobListing] = []
    offset = 0
    limit = 100
    url = SMARTRECRUITERS_POSTINGS_URL.format(company_id=company_id.strip())
    try:
        while True:
            payload = request_json(
                http,
                url,
                params={"limit": limit, "offset": offset},
                max_retries=max_retries,
                backoff_seconds=backoff_seconds,
                validate=_as_mapping,
            )
            postings = payload.get("content", [])
            for item in postings:
                try:
                    location = item.get("location") or {}
                    location_parts = [
                        str(location.get(key, "")).strip()
                        for key in ("city", "region", "country")
                        if location.get(key)
                    ]
                    posting_id = str(item["id"]).strip()
                    description = _fetch_description(
                        http,
                        company_id.strip(),
                        posting_id,
                        max_retries=max_retries,
                        backoff_seconds=backoff_seconds,
                    )
                    jobs.append(
                        JobListing(
                            source="smartrecruiters",
                            platform="smartrecruiters",
                            company=company,
                            title=str(item["name"]).strip(),
                            url=(
                                "https://jobs.smartrecruiters.com/"
                                f"{company_id.strip()}/{quote(posting_id, safe='')}"
                            ),
                            location_raw=", ".join(location_parts),
                            description=description,
                        )
                    )
                except (AttributeError, KeyError, TypeError) as error:
                    LOGGER.warning(
                        "Skipping malformed SmartRecruiters listing for %s: %s",
                        company,
                        error,
                    )
            if len(postings) < limit:
                break
            offset += limit
    finally:
        if owns_client:
            http.close()
    return jobs


def _as_mapping(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("content", []), list):
        raise ValueError("SmartRecruiters response must contain a content list")
    return payload


def _fetch_description(
    client: httpx.Client,
    company_id: str,
    posting_id: str,
    *,
    max_retries: int,
    backoff_seconds: float,
) -> str:
    details_url = (
        f"https://api.smartrecruiters.com/v1/companies/{quote(company_id, safe='')}"
        f"/postings/{quote(posting_id, safe='')}"
    )
    try:
        details = request_json(
            client,
            details_url,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            validate=_as_detail_mapping,
        )
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as error:
        LOGGER.warning("Could not fetch SmartRecruiters job details %s: %s", posting_id, error)
        return ""
    job_ad = details.get("jobAd") or {}
    sections = job_ad.get("sections") or {}
    if isinstance(sections, dict):
        text_parts = [
            str(section.get("text", "")).strip()
            for section in sections.values()
            if isinstance(section, dict) and section.get("text")
        ]
        if text_parts:
            return "\n\n".join(text_parts)
    return str(details.get("description") or "").strip()


def _as_detail_mapping(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("SmartRecruiters posting detail must be an object")
    return payload
