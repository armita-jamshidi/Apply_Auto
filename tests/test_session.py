"""Tests for database engine construction."""

import pytest

from db import session


def capture_create_engine(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        session, "create_engine", lambda url, **kwargs: calls.append({"url": url, **kwargs})
    )
    return calls


def test_postgres_engine_bounds_connection_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_create_engine(monkeypatch)

    session.create_database_engine("postgresql+psycopg://user:pw@localhost:5432/jobs")

    assert calls[0]["connect_args"] == {
        "connect_timeout": session.POSTGRES_CONNECT_TIMEOUT_SECONDS
    }
    assert calls[0]["pool_pre_ping"] is True


def test_sqlite_engine_gets_no_postgres_options(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_create_engine(monkeypatch)

    session.create_database_engine("sqlite+pysqlite:///:memory:")

    assert calls[0]["connect_args"] == {}
