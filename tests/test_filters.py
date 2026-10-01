"""Tests for location scope and duplicate job handling."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from agent.filters import (
    find_non_nc_workplace_dealbreakers,
    is_ambiguous_location,
    normalize_location,
    persist_job_if_new,
)
from agent.types import JobListing
from db.models import Base, Job


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Remote - US", "remote_us"),
        ("Remote, United States", "remote_us"),
        ("United States", "remote_us"),
        ("Anywhere in the US", "remote_us"),
        ("Remote - United States of America", "remote_us"),
        ("Remote - California", "other"),
        ("Remote - CA", "other"),
        ("Remote, EMEA", "other"),
        ("Remote - Canada", "other"),
        ("Remote", "other"),
        ("Hybrid (Durham, NC)", "nc"),
        ("Hybrid (Raleigh, North Carolina)", "nc"),
        ("Raleigh, NC", "nc"),
        ("Charlotte, North Carolina", "nc"),
        ("Research Triangle Park", "nc"),
        ("RTP", "nc"),
        ("Winston-Salem, NC", "nc"),
        ("Remote - New York", "other"),
        ("Seattle, WA", "other"),
        ("", "other"),
    ],
)
def test_normalize_location(location: str, expected: str) -> None:
    assert normalize_location(location) == expected


def test_hybrid_north_carolina_can_be_disabled() -> None:
    assert normalize_location("Hybrid (Durham, NC)", include_hybrid_nc=False) == "other"


def test_remote_us_north_carolina_is_in_scope() -> None:
    assert normalize_location("Remote - US, North Carolina") == "nc"


@pytest.mark.parametrize("location", ["Multiple locations", "Flexible", "Location flexible"])
def test_vague_location_is_marked_for_manual_review(location: str) -> None:
    assert is_ambiguous_location(location)


def test_specific_location_is_not_marked_ambiguous() -> None:
    assert not is_ambiguous_location("Remote - US")


@pytest.mark.parametrize(
    "description",
    [
        "Hybrid in San Francisco, CA three days per week.",
        "This role requires in-office presence in New York.",
    ],
)
def test_non_nc_hybrid_or_office_location_is_a_dealbreaker(description: str) -> None:
    dealbreakers = find_non_nc_workplace_dealbreakers(description)
    assert len(dealbreakers) == 1
    assert "outside North Carolina" in dealbreakers[0]


@pytest.mark.parametrize(
    "description",
    [
        "Hybrid in Raleigh, NC three days per week.",
        "Remote role; occasional collaboration with the San Francisco office.",
        "Hybrid role with location flexibility.",
    ],
)
def test_nc_or_non_requirement_location_is_not_a_dealbreaker(description: str) -> None:
    assert find_non_nc_workplace_dealbreakers(description) == []


def test_persist_job_skips_duplicate_url() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    listing = JobListing(
        source="greenhouse",
        platform="greenhouse",
        company="Example Co",
        title="Software Engineer",
        url="https://boards.greenhouse.io/example/jobs/123",
        location_raw="Remote - US",
        description="Build useful software.",
    )

    with Session(engine) as session:
        assert persist_job_if_new(session, listing, "other", status="queued") is True
        session.commit()
        assert session.scalar(select(Job.status).where(Job.url == listing.url)) == "queued"
        assert persist_job_if_new(session, listing, "other", status="queued") is False
        assert len(session.scalars(select(Job)).all()) == 1

    engine.dispose()
