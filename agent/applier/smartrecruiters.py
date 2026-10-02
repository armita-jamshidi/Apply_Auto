"""SmartRecruiters application form adapter."""

from pathlib import Path

from anthropic import Anthropic
from playwright.sync_api import Page

from agent.applier.base import run_tier1_dry_run
from agent.applier.greenhouse import ApplierResult
from agent.types import JobListing


def run_smartrecruiters_application(
    page: Page,
    job: JobListing,
    profile_path: Path,
    resume_path: Path,
    screenshot_path: Path,
    *,
    answers_client: Anthropic | None = None,
    fill_reviewed_motivation_drafts: bool = False,
    submit_live: bool = False,
) -> ApplierResult:
    """Fill a SmartRecruiters application; live submit requires explicit authorization."""
    return run_tier1_dry_run(
        page,
        job,
        profile_path,
        resume_path,
        screenshot_path,
        answers_client=answers_client,
        fill_reviewed_motivation_drafts=fill_reviewed_motivation_drafts,
        submit_live=submit_live,
    )
