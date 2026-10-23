"""Tests for keeping only US companies, or companies abroad with a US office."""

import dataclasses
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import main as discovery
from agent import pipeline
from agent.filters import non_us_company_reason
from agent.settings import load_settings
from agent.tracking import READY_FOR_YOU
from agent.types import JobListing
from db.models import Base, Job

ABROAD = "Company is based outside the US with no US office"


@pytest.mark.parametrize(
    ("description", "kept"),
    [
        ("Build agents for clinicians.", True),
        ("We are headquartered in Boston, with a team in Berlin.", True),
        ("Headquartered in Durham, North Carolina.", True),
        ("Our team is based in the US.", True),
        ("Founded in Paris by two doctors.", True),
        ("Evidence-based medicine for patients.", True),
        # Based abroad with a US office.
        ("A Berlin-based startup with offices in New York.", True),
        ("We are a London-based team. Our US office is in Boston.", True),
        ("Offices in London, Paris and New York.", True),
        ("We are based in Tel Aviv and have a U.S. entity. Join us.", True),
        ("Based in Toronto with a New York office.", True),
        ("<p>A <b>Paris-based</b> startup.</p><p>We have offices in San Francisco.</p>", True),
        # Based abroad, no US office.
        ("We are headquartered in London.", False),
        ("Our HQ: Toronto. Remote in the US.", False),
        ("A fully remote company headquartered in the UK, hiring in the US.", False),
        ("HQ in London, hiring across the US.", False),
        ("<p>We are a Bengaluru-based health-tech company.</p>", False),
    ],
)
def test_companies_abroad_need_a_us_office(description: str, kept: bool) -> None:
    reason = non_us_company_reason(description, "remote_us")
    assert (reason is None) is kept
    if not kept:
        assert reason == ABROAD


def test_a_role_in_north_carolina_is_at_a_us_office() -> None:
    assert non_us_company_reason("We are headquartered in London.", "nc") is None


def test_setting_is_on_by_default_and_can_be_turned_off(tmp_path: Path) -> None:
    assert load_settings().us_companies_only is True
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("database_url: 'sqlite:///:memory:'\n", encoding="utf-8")
    assert load_settings(settings_path, load_env=False).us_companies_only is False


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


def test_cleanup_removes_companies_abroad_even_when_prepared(session: Session) -> None:
    abroad = "Build agents for clinicians. We are headquartered in London."
    us = add_job(session, "https://jobs.lever.co/a/us")
    prepared = add_job(
        session, "https://jobs.lever.co/a/uk", status=READY_FOR_YOU, description=abroad
    )
    in_nc = add_job(
        session,
        "https://jobs.lever.co/a/nc",
        location_raw="Durham, NC",
        location_category="nc",
        description=abroad,
    )
    applied = add_job(
        session, "https://jobs.lever.co/a/applied", status="applied", description=abroad
    )

    assert pipeline.remove_out_of_scope_jobs(session, load_settings()) == 1

    assert prepared.status == "removed" and prepared.dealbreakers == [ABROAD]
    assert us.status == "new" and in_nc.status == "new" and applied.status == "applied"

    off = dataclasses.replace(load_settings(), us_companies_only=False)
    prepared.status = "new"
    assert pipeline.remove_out_of_scope_jobs(session, off) == 0


def test_discovery_skips_companies_abroad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"database_url: 'sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}'\n"
        "discovery:\n  us_companies_only: true\n",
        encoding="utf-8",
    )
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  - name: Lever Example\n    platform: lever\n    board: lever-example\n",
        encoding="utf-8",
    )

    def listing(slug: str, description: str) -> JobListing:
        return JobListing(
            source="lever",
            platform="lever",
            company="Lever Example",
            title=f"AI Engineer {slug}",
            url=f"https://jobs.lever.co/example/{slug}",
            location_raw="Remote - United States",
            description=description,
        )

    listings = [
        listing("Home", "Build agents."),
        listing("Abroad", "A London-based startup."),
        listing("Office", "A London-based startup with offices in Boston."),
    ]
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("agent.settings.load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys, "argv", ["job-agent", "--settings", str(settings), "--companies", str(companies)]
    )
    monkeypatch.setattr(discovery, "fetch_lever_jobs", lambda *_args, **_kwargs: listings)

    assert discovery.main() == 0

    output = capsys.readouterr().out
    assert "AI Engineer Home" in output
    assert "AI Engineer Office" in output
    assert "AI Engineer Abroad" not in output
