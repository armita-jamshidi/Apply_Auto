"""Shared typed values used by job sources and filters."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

LocationCategory = Literal["remote_us", "nc", "other"]
# Applicant tracking systems whose forms the applier can fill, and the hosts it accepts.
FILLABLE_HOSTS = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("jobs.lever.co", "jobs.eu.lever.co"),
    "ashby": ("jobs.ashbyhq.com",),
    "smartrecruiters": ("jobs.smartrecruiters.com",),
}
FILLABLE_PLATFORMS = frozenset(FILLABLE_HOSTS)


def is_fillable(platform: str, url: str) -> bool:
    """Whether the applier can open this job's form: a supported platform on its own host."""
    host = (urlsplit(url).hostname or "").casefold()
    return url.startswith("https://") and any(
        host == allowed or host.endswith("." + allowed)
        for allowed in FILLABLE_HOSTS.get(platform, ())
    )


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
