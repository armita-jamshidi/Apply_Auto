"""SQLAlchemy models for discovered jobs and application attempts."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all persisted models."""


class Job(Base):
    """A job listing discovered from a supported source."""

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("url", name="uq_jobs_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    location_raw: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    location_category: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fit_score: Mapped[int | None] = mapped_column(Integer)
    fit_reasons: Mapped[list[str] | None] = mapped_column(JSON)
    dealbreakers: Mapped[list[str] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    applications: Mapped[list["Application"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class Application(Base):
    """An application attempt and the answers used for that attempt."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    tailored_resume_path: Mapped[str | None] = mapped_column(String(2048))
    screenshot_path: Mapped[str | None] = mapped_column(String(2048))
    error: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[Job] = relationship(back_populates="applications")
