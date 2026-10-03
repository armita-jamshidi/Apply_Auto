"""Keep every test away from the real local database and dashboard."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_database_and_dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setattr("agent.dashboard.DASHBOARD_PATH", tmp_path / "dashboard.html")
