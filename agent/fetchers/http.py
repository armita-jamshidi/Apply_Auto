"""Shared retry policy for public ATS APIs."""

import logging
import time
from collections.abc import Callable
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
