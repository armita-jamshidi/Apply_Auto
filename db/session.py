"""Database engine and session construction."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine with stale-connection checks enabled."""
    return create_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a typed session factory bound to an engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)
