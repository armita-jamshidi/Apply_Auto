"""Shared retry policy for public ATS APIs."""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

import httpx

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


def request_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str | int | bool] | None = None,
    max_retries: int = 3,
    backoff_seconds: float = 0.5,
    validate: Callable[[object], T],
) -> T:
    """GET and validate JSON, retrying network, 429, and server errors with backoff."""
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")
    for attempt in range(max_retries):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            return validate(response.json())
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            retryable = not isinstance(error, httpx.HTTPStatusError) or (
                error.response.status_code == 429 or error.response.status_code >= 500
            )
            if not retryable or attempt + 1 == max_retries:
                raise
            delay = backoff_seconds * (2**attempt)
            LOGGER.warning("ATS request failed; retrying in %.1fs: %s", delay, error)
            time.sleep(delay)
    raise RuntimeError("ATS retry loop ended unexpectedly")


def label_remote(location: str, is_remote: bool) -> str:
    """Prefix "Remote - " when the board flags a job remote but its label does not say so."""
    location = " ".join(str(location).split())
    if not is_remote or "remote" in location.casefold():
        return location
    return f"Remote - {location}" if location else "Remote"


def parse_posted(value: object) -> datetime | None:
    """Read a posting date: ISO 8601 text, RFC 2822 text, or epoch seconds or milliseconds."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, int | float) or str(value).isdigit():
            number = float(value)
            moment = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, UTC)
        else:
            text = str(value).strip()
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
