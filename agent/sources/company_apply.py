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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from html import unescape
from urllib.parse import parse_qs, urlsplit

import httpx

from agent.fetchers.ashby import fetch_ashby_jobs
from agent.fetchers.greenhouse import fetch_greenhouse_jobs
from agent.fetchers.lever import fetch_lever_jobs
from agent.sources.new_grad_list import board_from_url
from agent.types import FILLABLE_PLATFORMS, JobListing

LOGGER = logging.getLogger(__name__)
BoardFetcher = Callable[..., list[JobListing]]
USER_AGENT = "job-agent/0.1 (personal job search)"


TitleReader = Callable[[dict, str], list[tuple[str, str]]]


def _light_fetcher(platform: str, url: str, read: TitleReader) -> BoardFetcher:
    """A board fetcher that lists titles and job pages only (no descriptions)."""

    def fetch(board: str, company: str, **_kwargs) -> list[JobListing]:
        response = httpx.get(
            url.format(board=board), timeout=15, headers={"User-Agent": USER_AGENT}
        )
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200 or "json" not in content_type:
            return []
        return [
            JobListing(platform, platform, company, title, link, "", "")
            for title, link in read(response.json(), board)
            if title and link
        ]

    return fetch


def _smartrecruiters_titles(data: dict, board: str) -> list[tuple[str, str]]:
    return [
        (item.get("name", ""), f"https://jobs.smartrecruiters.com/{board}/{item.get('id', '')}")
        for item in data.get("content", [])
    ]


def _workable_titles(data: dict, _board: str) -> list[tuple[str, str]]:
    return [(item.get("title", ""), item.get("url", "")) for item in data.get("jobs", [])]


def _recruitee_titles(data: dict, _board: str) -> list[tuple[str, str]]:
    return [(item.get("title", ""), item.get("careers_url", "")) for item in data.get("offers", [])]


def _bamboohr_titles(data: dict, board: str) -> list[tuple[str, str]]:
    return [
        (item.get("jobOpeningName", ""), f"https://{board}.bamboohr.com/careers/{item['id']}")
        for item in data.get("result", [])
        if item.get("id")
    ]


BOARD_FETCHERS: dict[str, BoardFetcher] = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
    # Titles only: SmartRecruiters' own fetcher reads every description, which is slow.
    "smartrecruiters": _light_fetcher(
        "smartrecruiters",
        "https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100",
        _smartrecruiters_titles,
    ),
    # These careers sites cannot be filled; a match links to the company's own job page.
    "workable": _light_fetcher(
        "workable", "https://apply.workable.com/api/v1/widget/accounts/{board}", _workable_titles
    ),
    "recruitee": _light_fetcher(
        "recruitee", "https://{board}.recruitee.com/api/offers/", _recruitee_titles
    ),
    "bamboohr": _light_fetcher(
        "bamboohr", "https://{board}.bamboohr.com/careers/list", _bamboohr_titles
    ),
}
_URL = re.compile(r"""https?://[^\s"'<>)\]]+""", re.IGNORECASE)
_CAREERS_LINK = re.compile(r"\b(?:careers?|jobs?|apply|join|hiring|work-with-us)\b", re.I)
# Hosts that are never the employer's own site.
_NOT_COMPANY_HOSTS = (
    "weworkremotely.com", "ycombinator.com", "linkedin.com", "github.com", "twitter.com",
    "x.com", "youtube.com", "medium.com", "google.com", "imgix.net", "amazonaws.com",
    "wellfound.com", "angel.co", "glassdoor.com", "indeed.com", "facebook.com",
    "instagram.com", "tiktok.com", "bit.ly", "himalayas.app", "simplyhired.com",
    "ziprecruiter.com", "builtin.com", "dice.com", "monster.com", "careerbuilder.com",
    "remoteok.com", "remotive.com", "workingnomads.com", "jobicy.com", "otta.com",
    "welcometothejungle.com",
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
    # The company's own page for a board job hosted on the company's site, if different.
    apply_url: str | None = None


class BoardCache:
    """Fetch each public board at most once per run; a missing board is remembered too."""

    def __init__(self, fetchers: dict[str, BoardFetcher] | None = None) -> None:
        self.fetchers = fetchers or BOARD_FETCHERS
        self._boards: dict[tuple[str, str], list[JobListing]] = {}

    def prefetch(self, boards: Iterable[tuple[str, str]]) -> None:
        """Fetch several boards at once; each lookup is a separate public API request."""
        missing = list(
            dict.fromkeys(b for b in boards if (b[0], b[1].casefold()) not in self._boards)
        )
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda board: self.jobs(*board), missing))

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
        job_url = board_job_url(link)
        if job_url is not None:
            return CompanyApplication(job_url[1], job_url[0])

    boards = _candidate_boards(company, links)
    cache.prefetch(boards)
    for platform, board in boards:
        for job in cache.jobs(platform, board):
            if titles_match(title, job.title):
                fillable = platform in FILLABLE_PLATFORMS
                return CompanyApplication(
                    job.url,
                    platform if fillable else None,
                    job.description if fillable else "",
                    job.apply_url if fillable else None,
                )

    careers = [
        link
        for link in links
        if (host := (urlsplit(link).hostname or "").casefold())
        and not _is_aggregator(host)
        and _CAREERS_LINK.search(link)
    ]
    # A link to the role's own page beats the company's general careers page.
    careers.sort(key=is_careers_listing)
    return CompanyApplication(careers[0]) if careers else None


# Path words of a general careers or openings page, as opposed to one role's page.
_LISTING_WORDS = frozenset(
    "careers career jobs job open-positions open-roles openings positions join join-us "
    "work-with-us company about search opportunities vacancies hiring all en en-us".split()
)


def is_careers_listing(url: str) -> bool:
    """Whether a link is a company's general careers or openings page, not one role.

    "https://acme.com/careers/" and "https://acme.com/jobs#open" are; a path or query
    naming a role ("/careers/ai-engineer", "?jobId=123") is not.
    """
    parts = urlsplit(url)
    segments = [segment.casefold() for segment in parts.path.split("/") if segment]
    return all(segment in _LISTING_WORDS for segment in segments) and not re.search(
        r"\d", parts.query
    )


def resolve_listing(listing: JobListing, cache: BoardCache) -> JobListing:
    """Point a remote-board or forum job at the company's own application when one is found.

    A job found on a supported board takes that board's URL and platform (keeping its source),
    so it can be filled; otherwise apply_url records the company's careers or apply page.
    """
    if listing.platform in FILLABLE_PLATFORMS or listing.apply_url is not None:
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
            apply_url=found.apply_url,
        )
    return replace(listing, apply_url=found.url)


def board_job_url(link: str) -> tuple[str, str] | None:
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


# Words that describe where or how a job is worked, not which job it is.
_TITLE_NOISE = frozenset(
    "remote fully worldwide anywhere global hybrid us usa contract contractor full part time "
    "ft pt opportunity position role opening the a an and of for 100".split()
)
_LEVELS = frozenset({"i", "ii", "iii", "iv", "v", "1", "2", "3", "4"})


def titles_match(posted: str, listed: str) -> bool:
    """Whether two titles name the same job.

    Word order, case, punctuation, parenthetical notes such as "(100% Remote)", text after
    " | ", and work-mode words are ignored. Every other word must match, so "Agent Engineer"
    is not "Senior Agent Engineer". A title listing several levels ("Engineer II/III")
    matches one of them ("Engineer III").
    """
    wanted, found = _title_words(posted), _title_words(listed)
    if not wanted or not found:
        return False
    if wanted == found:
        return True
    extra = wanted - found
    return found < wanted and extra <= _LEVELS and bool(found & _LEVELS)


def _title_words(title: str) -> frozenset[str]:
    core = re.sub(r"[(\[].*?[)\]]", " ", title.split(" | ")[0].casefold())
    return frozenset(word for word in re.findall(r"[a-z0-9]+", core) if word not in _TITLE_NOISE)
