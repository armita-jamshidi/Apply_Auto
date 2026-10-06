"""Keep every test away from the real local database, dashboard, and network lists."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_database_and_dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setattr("agent.dashboard.DASHBOARD_PATH", tmp_path / "dashboard.html")
    # Your own writing samples and kits must not leak into tests.
    monkeypatch.setattr("agent.library.WRITING_DIR", tmp_path / "writing_samples")
    # Discovery would otherwise download the public new-grad list and every board on it.
    monkeypatch.setattr("agent.main.fetch_new_grad_companies", lambda **_kwargs: [])
