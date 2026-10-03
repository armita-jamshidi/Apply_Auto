"""Database engine and session construction."""

from sqlalchemy import Engine, create_engine, inspect, text
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
    """Create missing tables and add missing optional columns to existing ones.

    A local SQLite file has no Alembic history, so later nullable columns are added here.
    """
    from db.models import Base

    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or not column.nullable:
                    continue
                column_type = column.type.compile(dialect=engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}')
                )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a typed session factory bound to an engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)
