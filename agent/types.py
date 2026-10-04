"""Shared typed values used by job sources and filters."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

LocationCategory = Literal["remote_us", "nc", "other"]
# Applicant tracking systems whose forms the applier can fill.
FILLABLE_PLATFORMS = frozenset({"greenhouse", "lever", "ashby", "smartrecruiters"})


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
    # Links found in the posting, used to find the company's own application.
    links: tuple[str, ...] = ()
    # The company's own careers or apply page, when the job is not on a fillable board.
    apply_url: str | None = None
    # When the employer published the posting, if the source says.
    posted_at: datetime | None = None
