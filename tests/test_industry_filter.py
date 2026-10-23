"""Tests for keeping only healthcare and health-tech roles (industry_keywords)."""

import dataclasses
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import pipeline
from agent.settings import excluded_role_reason, in_wanted_industry, load_settings
from agent.tracking import READY_FOR_YOU
from db.models import Base, Job


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def add_job(db: Session, url: str, **fields) -> Job:
    values = {
        "source": "lever",
        "platform": "lever",
        "company": "Example Health",
        "title": "AI Engineer",
        "url": url,
        "location_raw": "Remote - US",
        "location_category": "remote_us",
        "description": "Build agents that help clinicians.",
        "status": "new",
        **fields,
    }
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


@pytest.mark.parametrize(
    ("title", "description", "wanted"),
    [
        ("AI Engineer", "Build agents that summarize patient visits.", True),
        ("AI Engineer", "<p>We are a <b>digital health</b> company.</p>", True),
        ("AI Engineer", "<li>Experience with HIPAA and FHIR</li>", True),
        ("Clinical AI Engineer", "Build agents.", True),
        ("Machine Learning Engineer", "Our models speed up drug discovery.", True),
        ("AI Engineer", "Build agents for online stores.", False),
        # Benefits are not the company's field.
        ("AI Engineer", "Build agents. We offer medical, dental, and vision insurance.", False),
        ("AI Engineer", "<ul><li>Health care benefits</li><li>401(k)</li></ul>", False),
        # "Patients" inside another word does not count.
        ("AI Engineer", "Impatient for results? Build ad agents.", False),
    ],
)
def test_only_healthcare_roles_are_in_the_wanted_industry(
    title: str, description: str, wanted: bool
) -> None:
    assert in_wanted_industry(title, description, load_settings()) is wanted


def test_excluded_role_reason_names_the_industry() -> None:
    settings = load_settings()
    reason = excluded_role_reason("AI Engineer", "Build agents for online stores.", settings)
    assert reason == "Not a healthcare or health-tech role"
    # Too many years is reported first: it is the stronger reason.
    assert "Requires 5+ years" in excluded_role_reason(
        "AI Engineer", "5+ years of experience. Build ad agents.", settings
    )


def test_a_blank_description_waits_for_the_full_posting() -> None:
    # Discovery checks some titles before the description is fetched.
    assert excluded_role_reason("AI Engineer", "", load_settings()) is None


def test_entry_level_healthcare_roles_are_kept() -> None:
    settings = load_settings()
    assert settings.max_years_experience == 2
    keep = "1-2 years of experience. Build agents for hospitals."
    assert excluded_role_reason("AI Engineer", keep, settings) is None
    drop = "3+ years of experience. Build agents for hospitals."
    assert excluded_role_reason("AI Engineer", drop, settings) is not None


def test_no_industry_keywords_keeps_every_industry(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "database_url: 'sqlite:///:memory:'\ndiscovery:\n  experience_levels: [early]\n",
        encoding="utf-8",
    )
    settings = load_settings(settings_path, load_env=False)
    assert settings.industry_keywords == ()
    assert in_wanted_industry("AI Engineer", "Build agents for online stores.", settings)


def test_industry_keywords_are_read_from_settings(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "database_url: 'sqlite:///:memory:'\n"
        "discovery:\n  experience_levels: [early]\n  industry_keywords: [' Fintech ', '']\n",
        encoding="utf-8",
    )
    settings = load_settings(settings_path, load_env=False)
    assert settings.industry_keywords == ("fintech",)
    assert in_wanted_industry("AI Engineer", "A fintech startup.", settings)
    assert not in_wanted_industry("AI Engineer", "A hospital.", settings)


def test_cleanup_removes_roles_outside_healthcare_even_when_prepared(
    session: Session,
) -> None:
    health = add_job(session, "https://jobs.lever.co/a/health", status=READY_FOR_YOU)
    retail = add_job(
        session,
        "https://jobs.lever.co/a/retail",
        status=READY_FOR_YOU,
        company="Example Retail",
        description="Build agents for online stores.",
    )
    applied = add_job(
        session,
        "https://jobs.lever.co/a/applied",
        status="applied",
        description="Build agents for online stores.",
    )

    assert pipeline.remove_out_of_scope_jobs(session, load_settings()) == 1

    assert health.status == READY_FOR_YOU
    assert retail.status == "removed"
    assert retail.dealbreakers == ["Not a healthcare or health-tech role"]
    assert applied.status == "applied"


def test_cleanup_keeps_every_industry_when_the_filter_is_off(session: Session) -> None:
    retail = add_job(
        session, "https://jobs.lever.co/a/retail", description="Build agents for online stores."
    )
    settings = dataclasses.replace(load_settings(), industry_keywords=())

    assert pipeline.remove_out_of_scope_jobs(session, settings) == 0
    assert retail.status == "new"
