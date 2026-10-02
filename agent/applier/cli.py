"""Command-line launcher for a visible, dry-run-only Greenhouse application fill."""

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from agent.applier.greenhouse import run_greenhouse_dry_run
from agent.settings import PROJECT_ROOT
from agent.types import JobListing

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for a local dry run."""
    parser = argparse.ArgumentParser(
        description="Fill a Greenhouse form for review. This command never submits applications."
    )
    parser.add_argument("--job-url", required=True, help="HTTPS URL of the Greenhouse job form")
    parser.add_argument("--profile", type=Path, default=PROJECT_ROOT / "profile" / "profile.yaml")
    parser.add_argument(
        "--resume",
        type=Path,
        default=PROJECT_ROOT / "profile" / "resume.pdf",
    )
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--company", default="Unknown company")
    parser.add_argument("--title", default="Unknown role")
    return parser


def main() -> int:
    """Open the job form visibly, fill it, save a screenshot, and close without submitting."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    parsed_url = urlparse(args.job_url)
    if parsed_url.scheme != "https" or not (
        parsed_url.hostname == "greenhouse.io"
        or (parsed_url.hostname and parsed_url.hostname.endswith(".greenhouse.io"))
    ):
        raise SystemExit("--job-url must be an HTTPS Greenhouse URL")

    screenshot_path = args.screenshot or (
        PROJECT_ROOT
        / "screenshots"
        / f"greenhouse-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.png"
    )
    job = JobListing(
        source="greenhouse",
        platform="greenhouse",
        company=args.company,
        title=args.title,
        url=args.job_url,
        location_raw="",
        description="",
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            page = browser.new_page()
            result = run_greenhouse_dry_run(
                page,
                job,
                args.profile,
                args.resume,
                screenshot_path,
            )
        finally:
            browser.close()

    print(f"Status: {result.status}")
    print(f"Screenshot: {result.screenshot_path or 'not captured'}")
    print(f"Recorded answer fields: {len(result.answers)}")
    if result.error:
        print(f"Error: {result.error}")
    return 0 if result.status == "dry_run_ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
