"""Database engine and session construction."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

POSTGRES_CONNECT_TIMEOUT_SECONDS = 5


def create_database_engine(database_url: str) -> Engine:
    """Create an engine with stale-connection checks and a bounded Postgres connect wait."""
    connect_args: dict[str, int] = {}
    if make_url(database_url).get_backend_name() == "postgresql":
        # Without this, an unreachable host can stall a dry run for many minutes.
        connect_args["connect_timeout"] = POSTGRES_CONNECT_TIMEOUT_SECONDS
    return create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)


def ensure_schema(engine: Engine) -> None:
    """Create any missing tables (a fresh local SQLite file needs this; Postgres uses Alembic)."""
    from db.models import Base

    Base.metadata.create_all(engine)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a typed session factory bound to an engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)
