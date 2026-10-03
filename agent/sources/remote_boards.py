"""Remote job boards and hiring forums that publish jobs for automated reading.

- We Work Remotely: the public RSS feed (its robots.txt allows crawling).
- Hacker News "Who is hiring?": the monthly thread, through HN's public Algolia search API.
- Himalayas: the public job search API, first page of each query only (its robots.txt
  disallows the paged URLs).

Each returns JobListing records that go through the same title, location, and level filters
as company boards. Jobs whose region does not include the US are dropped here, and
US-eligible remote jobs are labelled "Remote - US (...)" so the location filter keeps them.
Boards that block automated access (for example SimplyHired's bot check) or whose robots.txt
disallows their API (for example Remotive) are not used.
"""

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from html import unescape

import httpx

from agent.fetchers.http import request_json
from agent.types import JobListing

LOGGER = logging.getLogger(__name__)
USER_AGENT = "job-agent/0.1 (personal job search)"
WWR_FEED_URL = "https://weworkremotely.com/remote-jobs.rss"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
HIMALAYAS_SEARCH_URL = "https://himalayas.app/jobs/api/search"
HIMALAYAS_QUERIES = (
    "ai engineer",
    "machine learning engineer",
    "llm engineer",
    "agent engineer",
    "applied ai engineer",
)
# Our experience levels mapped to Himalayas' seniority filter values.
HIMALAYAS_SENIORITY = {"early": "Entry-level", "mid": "Mid-level", "senior": "Senior"}
# Regions that include people working from the US.
_US_REGION = re.compile(
    r"\b(?:anywhere|worldwide|world|global|usa?|u\.s\.?|united states|north(?:ern)? america|"
    r"americas)\b",
    re.IGNORECASE,
)
_WORK_MODE = re.compile(r"\b(?:remote|hybrid|on-?site|in[- ]office)\b", re.IGNORECASE)
# Regions and time zones that mean a "REMOTE" post is not open to the US.
_NON_US_HINT = re.compile(
    r"\b(?:eu|europe|uk|emea|canada|india|apac|latam|cet|cest|gmt|bst|ist|london|berlin|"
    r"germany|paris|amsterdam)\b",
    re.IGNORECASE,
)


def remote_us_label(region: str) -> str | None:
    """Location label for a remote job open to the US, or None when the US is not included."""
    region = " ".join(unescape(region).split())
    if region and not _US_REGION.search(region):
        return None
    return f"Remote - US ({region})" if region else "Remote - US"


def fetch_weworkremotely(client: httpx.Client | None = None) -> list[JobListing]:
    """Read every current We Work Remotely job open to people in the US."""
    owns_client = client is None
    http = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    try:
        response = http.get(WWR_FEED_URL)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    finally:
        if owns_client:
            http.close()

    listings: list[JobListing] = []
    for item in root.iter("item"):
        heading = (item.findtext("title") or "").strip()
        company, _, title = heading.partition(":")
        if not title:
            company, title = "", heading
        location = remote_us_label(item.findtext("region") or "")
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        if location is None or not title.strip() or not link:
            continue
        listings.append(
            JobListing(
                source="weworkremotely",
                platform="weworkremotely",
                company=company.strip() or "Unknown company",
                title=title.strip(),
                url=link,
                location_raw=location,
                description=(item.findtext("description") or "").strip(),
            )
        )
    return listings


def fetch_hn_whos_hiring(
    client: httpx.Client | None = None, *, title_filter: Callable[[str], bool] | None = None
) -> list[JobListing]:
    """Read the latest Hacker News "Who is hiring?" thread.

    Posts start with a line like "Company | Role | Location | REMOTE". The role is the first
    part that title_filter accepts, and the location is every part naming a work mode or a
    place. Posts with no accepted role are skipped.
    """
    owns_client = client is None
    http = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    try:
        stories = request_json(
            http,
            HN_SEARCH_URL,
            params={"tags": "story,author_whoishiring", "query": "who is hiring"},
            max_retries=2,
            backoff_seconds=1,
            validate=_as_dict,
        )
        story = next(
            (
                hit
                for hit in stories.get("hits", [])
                if "who is hiring" in str(hit.get("title", "")).casefold()
            ),
            None,
        )
        if story is None:
            return []
        thread = request_json(
            http,
            HN_ITEM_URL.format(id=story["objectID"]),
            max_retries=2,
            backoff_seconds=1,
            validate=_as_dict,
        )
    finally:
        if owns_client:
            http.close()

    listings: list[JobListing] = []
    for comment in thread.get("children") or []:
        text = comment.get("text") or ""
        listing = _hn_listing(comment.get("id"), text, title_filter)
        if listing is not None:
            listings.append(listing)
    return listings


def _hn_listing(
    comment_id: object, html: str, title_filter: Callable[[str], bool] | None
) -> JobListing | None:
    first_line = unescape(re.sub(r"<[^>]+>", " ", re.split(r"<p>", html, maxsplit=1)[0]))
    parts = [" ".join(part.split()) for part in first_line.split("|")]
    parts = [part for part in parts if part]
    if len(parts) < 2 or comment_id is None:
        return None
    company, rest = parts[0], parts[1:]
    title = next(
        (part for part in rest if title_filter is None or title_filter(part)),
        None,
    )
    if title is None or len(title) > 150:
        return None
    places = [part for part in rest if part != title and (_WORK_MODE.search(part) or "," in part)]
    location = "; ".join(places)
    if re.search(r"\bremote\b", location, re.IGNORECASE) and not re.search(
        r"\b(?:us|usa|u\.s\.?|united states|north america|americas)\b", location, re.IGNORECASE
    ):
        # "REMOTE" with no region usually means anywhere; label it so the US filter keeps it,
        # unless the post limits it to another region or time zone.
        if not _NON_US_HINT.search(location):
            location = f"{remote_us_label('Anywhere')}; {location}"
    description = unescape(re.sub(r"<[^>]+>", " ", html.replace("<p>", "\n\n")))
    return JobListing(
        source="hackernews",
        platform="hackernews",
        company=company[:200],
        title=title,
        url=f"https://news.ycombinator.com/item?id={comment_id}",
        location_raw=location[:500],
        description=description.strip(),
    )


def fetch_himalayas(
    client: httpx.Client | None = None,
    *,
    experience_levels: tuple[str, ...] = ("early", "mid"),
    queries: tuple[str, ...] = HIMALAYAS_QUERIES,
    title_filter: Callable[[str], bool] | None = None,
) -> list[JobListing]:
    """Search Himalayas for each query at each experience level, US-eligible jobs only.

    Only the first page (20 jobs) of each search is read. Levels without a Himalayas
    seniority value ("unknown") are not searched unless no level maps, in which case each
    query is searched once without a seniority filter.
    """
    seniorities = [
        HIMALAYAS_SENIORITY[level] for level in experience_levels if level in HIMALAYAS_SENIORITY
    ] or [None]
    owns_client = client is None
    http = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    listings: dict[str, JobListing] = {}
    try:
        for query in queries:
            for seniority in seniorities:
                params: dict[str, str | int | bool] = {"q": query, "country": "United States"}
                if seniority:
                    params["seniority"] = seniority
                payload = request_json(
                    http,
                    HIMALAYAS_SEARCH_URL,
                    params=params,
                    max_retries=2,
                    backoff_seconds=1,
                    validate=_as_dict,
                )
                for job in payload.get("jobs") or []:
                    listing = _himalayas_listing(job, title_filter)
                    if listing is not None:
                        listings.setdefault(listing.url, listing)
    finally:
        if owns_client:
            http.close()
    return list(listings.values())


def _himalayas_listing(
    job: dict, title_filter: Callable[[str], bool] | None
) -> JobListing | None:
    title = " ".join(str(job.get("title") or "").split())
    url = str(job.get("applicationLink") or job.get("guid") or "").strip()
    if not title or not url or (title_filter is not None and not title_filter(title)):
        return None
    regions = [str(region) for region in job.get("locationRestrictions") or []]
    location = remote_us_label(", ".join(regions))
    if location is None:
        return None
    return JobListing(
        source="himalayas",
        platform="himalayas",
        company=str(job.get("companyName") or "").strip()[:200] or "Unknown company",
        title=title,
        url=url,
        location_raw=location[:500],
        description=str(job.get("description") or job.get("excerpt") or "").strip(),
    )


def _as_dict(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload
