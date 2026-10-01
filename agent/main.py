"""Command-line entry point for Phase 1 Greenhouse discovery."""

import argparse
import logging
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from agent.fetchers.greenhouse import fetch_greenhouse_jobs
from agent.filters import is_ambiguous_location, normalize_location, persist_job_if_new
from agent.settings import load_companies, load_settings
from db.models import Base
from db.session import create_database_engine, create_session_factory

LOGGER = logging.getLogger("job_agent")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the discovery command."""
    parser = argparse.ArgumentParser(description="Fetch and filter configured Greenhouse jobs.")
    parser.add_argument("--companies", type=Path, help="Path to companies.yaml")
    parser.add_argument("--settings", type=Path, help="Path to settings.yaml")
    return parser


def main() -> int:
    """Fetch configured boards, persist in-scope jobs, and print each result."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    settings = load_settings(args.settings)
    companies = load_companies(args.companies)
    if not companies:
        LOGGER.info("No companies configured; add Greenhouse board tokens to config/companies.yaml.")
        return 0

    engine = create_database_engine(settings.database_url)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    found_count = 0
    try:
        with session_factory() as session:
            for company in companies:
                if company.platform != "greenhouse":
                    LOGGER.info(
                        "Skipping %s (%s); Phase 1 fetches Greenhouse boards only.",
                        company.name,
                        company.platform,
                    )
                    continue
                try:
                    listings = fetch_greenhouse_jobs(
                        company.board or "",
                        company.name,
                        max_retries=settings.max_retries,
                        backoff_seconds=settings.backoff_seconds,
                    )
                except Exception:
                    LOGGER.exception("Could not fetch Greenhouse board for %s", company.name)
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
                        f"[{marker}] {listing.title} | {listing.company} | "
                        f"{category} | {listing.location_raw} | {listing.url}"
                    )
    finally:
        engine.dispose()

    LOGGER.info("Finished discovery; printed %d in-scope listings.", found_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
