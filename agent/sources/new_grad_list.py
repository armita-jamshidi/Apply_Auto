"""Find companies hiring new grads from the public SimplifyJobs New-Grad-Positions list.

The list is community-maintained on GitHub. Only its links to supported applicant tracking
systems are used, to learn each company's public board; the boards themselves are then
fetched through their official public APIs like any configured company.
"""

import logging
import re
from collections.abc import Iterable
from html import unescape
from urllib.parse import urlsplit

import httpx

from agent.settings import CompanyConfig

LOGGER = logging.getLogger(__name__)
NEW_GRAD_LIST_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"
)
_ROW = re.compile(r"<tr>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_LINK = re.compile(r'href="(https?://[^"]+)"', re.IGNORECASE)
# Path segment that names the board on each supported host.
_BOARD_HOSTS = {
    "job-boards.greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "jobs.smartrecruiters.com": "smartrecruiters",
}
_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def board_from_url(url: str) -> tuple[str, str] | None:
    """Return (platform, board) for a supported job-board link, or None."""
    parts = urlsplit(unescape(url))
    platform = _BOARD_HOSTS.get((parts.hostname or "").casefold())
    if platform is None:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        return None
    board = segments[0]
    if platform == "greenhouse" and board == "embed":
        query = dict(item.split("=", 1) for item in parts.query.split("&") if "=" in item)
        board = query.get("for", "")
    if platform == "smartrecruiters" and board == "oneclick-ui":
        board = segments[2] if len(segments) > 2 and segments[1] == "company" else ""
    return (platform, board) if _BOARD_TOKEN.match(board) else None


def companies_from_list(markdown: str) -> list[CompanyConfig]:
    """Parse the list's HTML table into one company per supported board."""
    found: dict[tuple[str, str], CompanyConfig] = {}
    company_name = ""
    for row in _ROW.findall(markdown):
        cells = _CELL.findall(row)
        if not cells:
            continue
        name = " ".join(unescape(re.sub(r"<[^>]+>", " ", cells[0])).split())
        # The list prefixes some companies with markers such as a fire emoji.
        name = re.sub(r"^[^\w(↳]+", "", name).strip()
        # A "↳" row continues the company named on the row above.
        if name and name != "↳":
            company_name = name
        for link in _LINK.findall(row):
            board = board_from_url(link)
            if board is None:
                continue
            key = (board[0], board[1].casefold())
            if key not in found and company_name:
                found[key] = CompanyConfig(
                    name=company_name, platform=board[0], board=board[1], url=None
                )
    return list(found.values())


def fetch_new_grad_companies(
    *,
    exclude: Iterable[CompanyConfig] = (),
    client: httpx.Client | None = None,
    url: str = NEW_GRAD_LIST_URL,
) -> list[CompanyConfig]:
    """Download the list and return companies whose boards are not already configured."""
    known = {
        (company.platform, (company.board or "").casefold())
        for company in exclude
        if company.board
    }
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        response = http.get(url)
        response.raise_for_status()
        markdown = response.text
    finally:
        if owns_client:
            http.close()
    companies = [
        company
        for company in companies_from_list(markdown)
        if (company.platform, (company.board or "").casefold()) not in known
    ]
    LOGGER.info("New-grad list added %d company boards", len(companies))
    return companies
