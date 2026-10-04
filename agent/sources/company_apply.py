"""Find where to apply for a job on the company's own site.

Remote job boards and forum posts link to their own pages, not the employer's application.
For those jobs this module looks for, in order:

1. an application link to a supported applicant tracking system (Greenhouse, Lever, Ashby,
   SmartRecruiters) inside the posting;
2. the same job on the company's public Greenhouse, Lever, or Ashby board, read through the
   board's public API, with the board name guessed from links in the posting or the
   company's name and matched by job title;
3. a careers or apply link on the company's own site inside the posting.

A job found on a supported board becomes an ordinary board job, so its form can be filled
and its answers prepared. No site is logged into, and pages behind sign-ins are not read.
"""

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from html import unescape
from urllib.parse import parse_qs, urlsplit

from agent.fetchers.ashby import fetch_ashby_jobs
from agent.fetchers.greenhouse import fetch_greenhouse_jobs
from agent.fetchers.lever import fetch_lever_jobs
from agent.sources.new_grad_list import board_from_url
from agent.types import FILLABLE_PLATFORMS, JobListing

LOGGER = logging.getLogger(__name__)
BoardFetcher = Callable[..., list[JobListing]]
BOARD_FETCHERS: dict[str, BoardFetcher] = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
}
_URL = re.compile(r"""https?://[^\s"'<>)\]]+""", re.IGNORECASE)
_CAREERS_LINK = re.compile(r"\b(?:careers?|jobs?|apply|join|hiring|work-with-us)\b", re.I)
# Hosts that are never the employer's own site.
_NOT_COMPANY_HOSTS = (
    "weworkremotely.com", "ycombinator.com", "linkedin.com", "github.com", "twitter.com",
    "x.com", "youtube.com", "medium.com", "google.com", "imgix.net", "amazonaws.com",
    "wellfound.com", "angel.co", "glassdoor.com", "indeed.com", "facebook.com",
    "instagram.com", "tiktok.com", "bit.ly",
)
_NAME_SUFFIXES = re.compile(
    r"\b(?:inc|llc|ltd|corp|corporation|co|company|labs?|technologies|technology|hq)\b\.?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CompanyApplication:
    """Where to apply: a supported board job (fillable) or a page on the company's site."""

    url: str
    platform: str | None = None
    description: str = ""


class BoardCache:
    """Fetch each public board at most once per run; a missing board is remembered too."""

    def __init__(self, fetchers: dict[str, BoardFetcher] | None = None) -> None:
        self.fetchers = fetchers or BOARD_FETCHERS
        self._boards: dict[tuple[str, str], list[JobListing]] = {}

    def jobs(self, platform: str, board: str) -> list[JobListing]:
        key = (platform, board.casefold())
        if key not in self._boards:
            try:
                self._boards[key] = self.fetchers[platform](
                    board, board, max_retries=1, backoff_seconds=0
                )
            except Exception as error:
                LOGGER.debug("No %s board %r: %s", platform, board, error)
                self._boards[key] = []
        return self._boards[key]


def links_in(*texts: str) -> list[str]:
    """Every URL in the texts (href attributes and bare links), in order, without repeats."""
    found: list[str] = []
    for text in texts:
        for url in _URL.findall(unescape(text or "")):
            url = url.rstrip(".,;:!?")
            if url not in found:
                found.append(url)
    return found


def find_company_application(
    company: str, title: str, links: Iterable[str], cache: BoardCache
) -> CompanyApplication | None:
    """Find the company's own application for a job, or None when there is no better link."""
    links = list(links)
    for link in links:
        job_url = _board_job_url(link)
        if job_url is not None:
            return CompanyApplication(job_url[1], job_url[0])

    wanted = _normalize_title(title)
    for platform, board in _candidate_boards(company, links):
        for job in cache.jobs(platform, board):
            if _normalize_title(job.title) == wanted:
                return CompanyApplication(job.url, platform, job.description)

    for link in links:
        host = (urlsplit(link).hostname or "").casefold()
        if host and not _is_aggregator(host) and _CAREERS_LINK.search(link):
            return CompanyApplication(link)
    return None


def resolve_listing(listing: JobListing, cache: BoardCache) -> JobListing:
    """Point a remote-board or forum job at the company's own application when one is found.

    A job found on a supported board takes that board's URL and platform (keeping its source),
    so it can be filled; otherwise apply_url records the company's careers or apply page.
    """
    if listing.platform in FILLABLE_PLATFORMS:
        return listing
    found = find_company_application(
        listing.company, listing.title, [*listing.links, *links_in(listing.description)], cache
    )
    if found is None:
        return listing
    if found.platform is not None:
        return replace(
            listing,
            platform=found.platform,
            url=found.url,
            description=found.description or listing.description,
            apply_url=None,
        )
    return replace(listing, apply_url=found.url)


def _board_job_url(link: str) -> tuple[str, str] | None:
    """(platform, job URL) when a link points at one job on a supported board."""
    board = board_from_url(link)
    if board is None:
        return None
    platform, token = board
    parts = urlsplit(unescape(link))
    segments = [segment for segment in parts.path.split("/") if segment]
    host = parts.hostname or ""
    if platform == "greenhouse":
        query = parse_qs(parts.query)
        if len(segments) >= 3 and segments[1] == "jobs" and segments[2].isdigit():
            return platform, f"https://{host}/{token}/jobs/{segments[2]}"
        job_id = (query.get("token") or query.get("gh_jid") or [""])[0]
        if job_id.isdigit():
            return platform, f"https://job-boards.greenhouse.io/{token}/jobs/{job_id}"
        return None
    if platform in {"lever", "ashby"} and len(segments) >= 2:
        return platform, f"https://{host}/{segments[0]}/{segments[1]}"
    if platform == "smartrecruiters" and len(segments) >= 2 and segments[0] != "oneclick-ui":
        return platform, f"https://{host}/{segments[0]}/{segments[1]}"
    return None


def _candidate_boards(company: str, links: list[str]) -> list[tuple[str, str]]:
    """Board names to try: boards linked in the posting, then guesses from site and name."""
    boards: list[tuple[str, str]] = []
    for link in links:
        board = board_from_url(link)
        if board is not None and board[0] in BOARD_FETCHERS and board not in boards:
            boards.append(board)
    tokens: list[str] = []
    for link in links:
        host = (urlsplit(link).hostname or "").casefold().removeprefix("www.")
        if host and not _is_aggregator(host) and "." in host:
            tokens.append(host.split(".")[-2])
    name = _NAME_SUFFIXES.sub(" ", company.casefold())
    words = re.findall(r"[a-z0-9]+", name)
    if words:
        tokens += ["".join(words), "-".join(words), words[0]]
    for token in dict.fromkeys(tokens):
        if len(token) < 3:
            continue
        for platform in BOARD_FETCHERS:
            if (platform, token) not in boards:
                boards.append((platform, token))
    return boards


def _is_aggregator(host: str) -> bool:
    return any(host == item or host.endswith("." + item) for item in _NOT_COMPANY_HOSTS) or (
        board_from_url(f"https://{host}/x") is not None
    )


def _normalize_title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.casefold()))
