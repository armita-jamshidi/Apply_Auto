"""Shared typed values used by job sources and filters."""

from dataclasses import dataclass
from typing import Literal

LocationCategory = Literal["remote_us", "nc", "other"]


@dataclass(frozen=True, slots=True)
class JobListing:
    """A normalized job record returned by a source fetcher."""

    source: str
    platform: str
    company: str
    title: str
    url: str
    location_raw: str
    description: str
