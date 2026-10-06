"""Find postings that have closed, so the dashboard never links to an empty page.

A closed job's link opens a "couldn't find anything here" page or a blank one. Each link
is requested once: a 404 or 410, or Greenhouse's redirect to "?error=true", means the
posting is gone. Ashby pages load even for closed jobs, so Ashby jobs are checked against
the board's public job list instead. Anything unclear (a block, a timeout, a 403)
leaves the job alone.
"""

import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.fetchers.greenhouse import greenhouse_job_id
from agent.sources.company_apply import BoardCache, _is_aggregator
from agent.sources.new_grad_list import board_from_url
from db.models import Job

LOGGER = logging.getLogger(__name__)
CLOSED = "closed"
USER_AGENT = "job-agent/0.1 (personal job search)"
CHECKED_STATUSES = ("new", "queued", "manual_review")


def closed_reason(job: Job, boards: BoardCache, client: httpx.Client) -> str | None:
    """Why a job's posting has closed, or None when it is open or the answer is unclear."""
    board = board_from_url(job.url)
    if board is not None and board[0] == "ashby" and job.platform == "ashby":
        # Ashby pages answer 200 even for closed jobs, so its public job list decides.
        listed = boards.jobs(*board)
        if listed and not any(_same_posting("ashby", job.url, item.url) for item in listed):
            return "No longer listed on its Ashby board."
        return None
    # Other boards answer a closed job with 404 (Lever, SmartRecruiters) or Greenhouse's
    # "?error=true" redirect; their lists can be paged, so they are not trusted alone.
    url = job.url if board is not None else job.apply_url or job.url
    host = (urlsplit(url).hostname or "").casefold()
    if not url.startswith("https://") or (board is None and _is_aggregator(host)):
        return None  # job boards and forums block or ignore automated checks
    try:
        response = client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code in (404, 410):
        return f"The posting returns HTTP {response.status_code}."
    if "error=true" in str(response.url) and "greenhouse.io" in str(response.url):
        return "Greenhouse says the posting no longer exists."
    return None


def close_finished_postings(
    session: Session,
    *,
    boards: BoardCache | None = None,
    client: httpx.Client | None = None,
    jobs: Iterable[Job] | None = None,
) -> int:
    """Mark open jobs whose postings have closed as closed; return how many."""
    jobs = list(
        jobs
        if jobs is not None
        else session.scalars(select(Job).where(Job.status.in_(CHECKED_STATUSES)))
    )
    if not jobs:
        return 0
    boards = boards or BoardCache()
    boards.prefetch(
        {
            board
            for job in jobs
            if (board := board_from_url(job.url)) is not None and board[0] == "ashby"
        }
    )
    owns_client = client is None
    http = client or httpx.Client(
        timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            reasons = list(pool.map(lambda job: closed_reason(job, boards, http), jobs))
    finally:
        if owns_client:
            http.close()
    closed = 0
    for job, reason in zip(jobs, reasons, strict=True):
        if reason is None:
            continue
        job.status = CLOSED
        closed += 1
        print(f"[closed] {job.company} | {job.title} | {reason}")
    session.commit()
    return closed


def _same_posting(platform: str, stored: str, listed: str) -> bool:
    found = _posting_id(platform, stored)
    return found is not None and found == _posting_id(platform, listed)


def _posting_id(platform: str, url: str) -> str | None:
    """A board job's id: the Greenhouse job number, or the path part after the board name."""
    if platform == "greenhouse":
        return greenhouse_job_id(url)
    segments = [unquote(part) for part in urlsplit(url).path.split("/") if part]
    if platform == "smartrecruiters" and segments and segments[0] == "oneclick-ui":
        return segments[-1]  # /oneclick-ui/company/<board>/publication/<id>
    if len(segments) < 2:
        return None
    # SmartRecruiters links may add the title to the id ("744000152455329-ai-developer").
    return segments[1].split("-")[0] if platform == "smartrecruiters" else segments[1].casefold()
