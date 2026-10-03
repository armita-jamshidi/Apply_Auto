"""Tests for the discovery command."""

import logging
import sys
from pathlib import Path

import httpx
import pytest

from agent import main as discovery


def test_parser_description_is_not_greenhouse_only() -> None:
    assert "Greenhouse" not in (discovery.build_parser().description or "")


def test_fetch_failure_log_names_the_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"database_url: 'sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}'\n",
        encoding="utf-8",
    )
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  - name: Lever Example\n    platform: lever\n    board: lever-example\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("agent.settings.load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys, "argv", ["job-agent", "--settings", str(settings), "--companies", str(companies)]
    )

    def failing_fetch(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(discovery, "fetch_lever_jobs", failing_fetch)

    with caplog.at_level(logging.ERROR, logger="job_agent"):
        assert discovery.main() == 0

    assert "Could not fetch lever board for Lever Example" in caplog.text
    assert "Greenhouse" not in caplog.text


def test_discovery_keeps_only_early_career_and_unstated_levels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent.types import JobListing

    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"database_url: 'sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}'\n",
        encoding="utf-8",
    )
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  - name: Lever Example\n    platform: lever\n    board: lever-example\n",
        encoding="utf-8",
    )

    def listing(title: str, description: str = "") -> JobListing:
        return JobListing(
            source="lever",
            platform="lever",
            company="Lever Example",
            title=title,
            url=f"https://jobs.lever.co/example/{title.replace(' ', '-').lower()}",
            location_raw="Remote - United States",
            description=description,
        )

    listings = [
        listing("Software Engineer, New Grad"),
        listing("Senior Software Engineer"),
        listing("Platform Engineer", "Requires 6+ years of experience."),
        listing("Support Engineer"),
    ]
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("agent.settings.load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys, "argv", ["job-agent", "--settings", str(settings), "--companies", str(companies)]
    )
    monkeypatch.setattr(discovery, "fetch_lever_jobs", lambda *_args, **_kwargs: listings)

    assert discovery.main() == 0

    output = capsys.readouterr().out
    assert "Software Engineer, New Grad | Lever Example | early" in output
    assert "Support Engineer | Lever Example | unknown" in output
    assert "Senior Software Engineer" not in output
    assert "Platform Engineer" not in output


def test_discovery_adds_new_grad_list_companies_unless_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.settings import CompanyConfig

    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"database_url: 'sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}'\n",
        encoding="utf-8",
    )
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  - name: Lever Example\n    platform: lever\n    board: lever-example\n",
        encoding="utf-8",
    )
    fetched: list[str] = []
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("agent.settings.load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        discovery,
        "fetch_new_grad_companies",
        lambda **_kwargs: [CompanyConfig("Listed Co", "lever", "listed-co", None)],
    )
    monkeypatch.setattr(
        discovery, "fetch_lever_jobs", lambda board, *_args, **_kwargs: fetched.append(board) or []
    )
    base_args = ["job-agent", "--settings", str(settings), "--companies", str(companies)]

    monkeypatch.setattr(sys, "argv", base_args)
    discovery.main()
    monkeypatch.setattr(sys, "argv", [*base_args, "--no-new-grad-list"])
    discovery.main()

    assert fetched == ["lever-example", "listed-co", "lever-example"]
