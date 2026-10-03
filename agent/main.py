"""Command-line entry point for Tier 1 ATS job discovery."""

import argparse
import functools
import logging
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from agent.dashboard import refresh_dashboard
from agent.fetchers.ashby import fetch_ashby_jobs
from agent.fetchers.greenhouse import fetch_greenhouse_jobs
from agent.fetchers.lever import fetch_lever_jobs
from agent.fetchers.smartrecruiters import fetch_smartrecruiters_jobs
from agent.filters import is_ambiguous_location, normalize_location, persist_job_if_new
from agent.seniority import classify_experience
from agent.settings import load_companies, load_settings
from agent.sources.new_grad_list import fetch_new_grad_companies
from db.models import Base
from db.session import create_database_engine, create_session_factory

LOGGER = logging.getLogger("job_agent")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the discovery command."""
    parser = argparse.ArgumentParser(
        description="Fetch and filter jobs from configured Tier 1 ATS boards."
    )
    parser.add_argument("--companies", type=Path, help="Path to companies.yaml")
    parser.add_argument("--settings", type=Path, help="Path to settings.yaml")
    parser.add_argument(
        "--no-new-grad-list",
        action="store_true",
        help="Only search configured companies, not the public new-grad list.",
    )
    return parser


def main() -> int:
    """Fetch configured boards, persist in-scope jobs, and print each result."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    settings = load_settings(args.settings)
    companies = load_companies(args.companies)
    if settings.include_new_grad_list and not args.no_new_grad_list:
        try:
            companies += fetch_new_grad_companies(exclude=companies)
        except Exception:
            LOGGER.exception("Could not load the new-grad list; using configured companies")
    if not companies:
        LOGGER.info(
            "No companies configured; add ATS boards to config/companies.yaml."
        )
        return 0

    engine = create_database_engine(settings.database_url)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    found_count = 0
    try:
        with session_factory() as session:
            for company in companies:
                fetcher = {
                    "greenhouse": fetch_greenhouse_jobs,
                    "lever": fetch_lever_jobs,
                    "ashby": fetch_ashby_jobs,
                    "smartrecruiters": fetch_smartrecruiters_jobs,
                }.get(company.platform)
                if fetcher is None:
                    LOGGER.info(
                        "Skipping %s (%s) until its fetcher is available.",
                        company.name,
                        company.platform,
                    )
                    continue
                if company.platform == "smartrecruiters":
                    fetcher = functools.partial(
                        fetcher,
                        include=functools.partial(_worth_describing, settings=settings),
                    )
                try:
                    listings = fetcher(
                        company.board or "",
                        company.name,
                        max_retries=settings.max_retries,
                        backoff_seconds=settings.backoff_seconds,
                    )
                except Exception:
                    LOGGER.exception(
                        "Could not fetch %s board for %s", company.platform, company.name
                    )
                    continue

                LOGGER.info("Fetched %d listings for %s", len(listings), company.name)
                for listing in listings:
                    category = normalize_location(
                        listing.location_raw,
                        include_hybrid_nc=settings.include_hybrid_nc,
                    )
                    ambiguous = is_ambiguous_location(listing.location_raw)
                    if category == "other" and not ambiguous:
                        continue
                    level = classify_experience(listing.title, listing.description).level
                    if level not in settings.experience_levels:
                        continue
                    status = "queued" if ambiguous else "new"
                    try:
                        is_new = persist_job_if_new(session, listing, category, status=status)
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        is_new = False
                        LOGGER.info("Duplicate listing ignored: %s", listing.url)
                    found_count += 1
                    marker = "new" if is_new else "already known"
                    if ambiguous:
                        marker = "manual review" if is_new else "manual review, already known"
                    print(
                        f"[{marker}] {listing.title} | {listing.company} | {level} | "
                        f"{category} | {listing.location_raw} | {listing.url}"
                    )
    finally:
        engine.dispose()

    LOGGER.info("Finished discovery; printed %d in-scope listings.", found_count)
    dashboard = refresh_dashboard()
    if dashboard is not None:
        print(f"Dashboard: {dashboard}")
    return 0


def _worth_describing(listing, *, settings) -> bool:
    """Cheap location and title checks before fetching a posting's description."""
    category = normalize_location(
        listing.location_raw, include_hybrid_nc=settings.include_hybrid_nc
    )
    if category == "other" and not is_ambiguous_location(listing.location_raw):
        return False
    level = classify_experience(listing.title).level
    return level in settings.experience_levels or level == "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
