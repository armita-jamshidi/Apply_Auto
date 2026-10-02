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
