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


def test_ensure_schema_adds_new_optional_columns_to_an_old_database(tmp_path) -> None:
    import sqlite3

    from sqlalchemy import inspect

    database = tmp_path / "old.db"
    with sqlite3.connect(database) as old:
        old.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, platform TEXT, company TEXT,"
            " title TEXT, url TEXT, location_raw TEXT, location_category TEXT, description TEXT,"
            " status TEXT, first_seen_at TEXT, updated_at TEXT)"
        )
    engine = session.create_database_engine(f"sqlite+pysqlite:///{database.as_posix()}")

    session.ensure_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
    assert {"experience_level", "min_years_experience", "fit_score"} <= columns
    assert "applications" in inspect(engine).get_table_names()
    engine.dispose()
