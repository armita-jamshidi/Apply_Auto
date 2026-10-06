"""Load local YAML configuration and environment overrides."""

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

from agent.seniority import classify_experience

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class CompanyConfig:
    """A company entry from config/companies.yaml."""

    name: str
    platform: str
    board: str | None
    url: str | None


# Roles the candidate does not qualify for: internships, co-ops, and new-grad programs.
DEFAULT_EXCLUDED_TITLES = (
    "intern", "interns", "internship", "internships", "co-op", "co-ops", "coop", "co op",
    "new grad", "new grads", "new-grad", "new graduate", "new graduates", "recent grad",
    "recent graduate", "university grad", "university graduate", "college grad", "college grads",
    "college graduate", "college graduates",
)
DEFAULT_EXCLUDED_DESCRIPTION_PHRASES = (
    "new grad", "new grads", "new-grad", "new graduate", "new graduates",
    "new college grad", "new college graduate",
)


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Runtime settings for job discovery and fit scoring."""

    database_url: str
    include_hybrid_nc: bool
    max_retries: int
    backoff_seconds: float
    fit_score_threshold: int
    anthropic_model: str
    daily_application_cap: int
    company_monthly_application_cap: int
    experience_levels: tuple[str, ...] = ("early", "unknown")
    include_new_grad_list: bool = True
    title_keywords: tuple[str, ...] = ()
    title_role_keywords: tuple[str, ...] = ()
    remote_boards: tuple[str, ...] = ()
    max_jobs_per_company: int | None = None
    max_years_experience: int | None = None
    max_posting_age_days: int | None = None
    form_agent_model: str = "claude-opus-5-5"
    careers_agent_model: str = "claude-sonnet-5-5"
    resume_tailor_model: str = "claude-opus-5-5"
    careers_agent_limit: int = 5
    fresh_posting_days: int = 7
    exclude_title_keywords: tuple[str, ...] = DEFAULT_EXCLUDED_TITLES
    exclude_description_phrases: tuple[str, ...] = DEFAULT_EXCLUDED_DESCRIPTION_PHRASES


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def load_settings(config_path: Path | None = None, *, load_env: bool = True) -> AgentSettings:
    """Load settings, allowing DATABASE_URL and .env to override YAML."""
    if load_env:
        load_dotenv(PROJECT_ROOT / ".env")
    raw = _read_yaml(config_path or PROJECT_ROOT / "config" / "settings.yaml")
    location = raw.get("location", {})
    network = raw.get("network", {})
    database_url = os.getenv("DATABASE_URL", raw.get("database_url", ""))
    fit_score_threshold = int(raw.get("fit_score_threshold", 70))
    anthropic_model = str(raw.get("anthropic_model", "claude-sonnet-5-5"))
    safeguards = raw.get("safeguards", {})
    daily_application_cap = int(safeguards.get("daily_application_cap", 5))
    company_monthly_application_cap = int(safeguards.get("company_monthly_application_cap", 5))
    if not database_url:
        raise ValueError("Set database_url in settings.yaml or DATABASE_URL in the environment")
    if not 0 <= fit_score_threshold <= 100:
        raise ValueError("fit_score_threshold must be between 0 and 100")
    if daily_application_cap < 1 or company_monthly_application_cap < 1:
        raise ValueError("application caps must be at least 1")
    discovery = raw.get("discovery", {}) or {}
    experience_levels = tuple(
        str(level).strip().lower()
        for level in discovery.get("experience_levels", ["early", "unknown"])
    )
    unknown_levels = set(experience_levels) - {"early", "mid", "senior", "unknown"}
    if unknown_levels or not experience_levels:
        raise ValueError("discovery.experience_levels must use early, mid, senior, or unknown")
    return AgentSettings(
        database_url=resolve_database_url(database_url),
        include_hybrid_nc=bool(location.get("include_hybrid_nc", True)),
        max_retries=int(network.get("max_retries", 3)),
        backoff_seconds=float(network.get("backoff_seconds", 0.5)),
        fit_score_threshold=fit_score_threshold,
        anthropic_model=anthropic_model,
        daily_application_cap=daily_application_cap,
        company_monthly_application_cap=company_monthly_application_cap,
        experience_levels=experience_levels,
        include_new_grad_list=bool(discovery.get("include_new_grad_list", True)),
        title_keywords=tuple(
            str(keyword).strip().casefold()
            for keyword in discovery.get("title_keywords") or []
            if str(keyword).strip()
        ),
        title_role_keywords=_keywords(discovery.get("title_role_keywords"), ()),
        remote_boards=_keywords(discovery.get("remote_boards"), ()),
        form_agent_model=str(raw.get("form_agent_model") or "claude-opus-5-5"),
        careers_agent_model=str(raw.get("careers_agent_model") or anthropic_model),
        resume_tailor_model=str(raw.get("resume_tailor_model") or "claude-opus-5-5"),
        careers_agent_limit=int(discovery.get("careers_agent_limit", 5)),
        fresh_posting_days=int(discovery.get("fresh_posting_days", 7)),
        max_posting_age_days=(
            int(discovery["max_posting_age_days"])
            if discovery.get("max_posting_age_days")
            else None
        ),
        max_years_experience=(
            int(discovery["max_years_experience"])
            if discovery.get("max_years_experience") is not None
            else None
        ),
        max_jobs_per_company=(
            int(discovery["max_jobs_per_company"])
            if discovery.get("max_jobs_per_company")
            else None
        ),
        exclude_title_keywords=_keywords(
            discovery.get("exclude_title_keywords"), DEFAULT_EXCLUDED_TITLES
        ),
        exclude_description_phrases=_keywords(
            discovery.get("exclude_description_phrases"), DEFAULT_EXCLUDED_DESCRIPTION_PHRASES
        ),
    )


def _keywords(configured: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if configured is None:
        return default
    return tuple(str(item).strip().casefold() for item in configured if str(item).strip())


def title_in_scope(title: str, settings: AgentSettings) -> bool:
    """Whether a title names a wanted field (title_keywords) and kind of role (role keywords)."""
    return title_matches(title, settings.title_keywords) and title_matches(
        title, settings.title_role_keywords
    )


def stale_posting_reason(posted_at: datetime | None, settings: AgentSettings) -> str | None:
    """Why a posting is too old to keep, or None when it is recent or its date is unknown."""
    limit = settings.max_posting_age_days
    if limit is None or posted_at is None:
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - posted_at).days
    return f"Posted {age} days ago (older than {limit} days)" if age > limit else None


def excluded_role_reason(title: str, description: str, settings: AgentSettings) -> str | None:
    """Why a role is one the candidate has ruled out (intern, co-op, new grad), else None."""
    if settings.exclude_title_keywords and title_matches(
        title, settings.exclude_title_keywords
    ):
        return "Internship, co-op, or new-grad role (excluded in settings)"
    text = " ".join(re.sub(r"<[^>]+>", " ", description).split())
    if settings.exclude_description_phrases and title_matches(
        text, settings.exclude_description_phrases
    ):
        return "Posting is for new graduates (excluded in settings)"
    limit = settings.max_years_experience
    if limit is not None:
        experience = classify_experience(title, description)
        if experience.min_years is not None and experience.min_years > limit:
            return f"Requires {experience.min_years}+ years of experience (max {limit})"
        if experience.level == "mid" and experience.min_years is None:
            return f"Mid-level role that does not state it needs {limit} years or fewer"
    return None


def title_matches(title: str, keywords: tuple[str, ...]) -> bool:
    """Return whether a job title contains one of the keywords (all titles if none)."""
    if not keywords:
        return True
    text = " ".join(title.casefold().split())
    return any(re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) for keyword in keywords)


def resolve_database_url(database_url: str) -> str:
    """Anchor a relative SQLite file to the project folder and create its directory."""
    url = make_url(database_url)
    database = url.database or ""
    if url.get_backend_name() != "sqlite" or database in {"", ":memory:"}:
        return database_url
    path = Path(database)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    # Forward slashes avoid percent-encoding Windows drive letters and backslashes.
    return f"{url.drivername}:///{path.as_posix()}"


def load_companies(config_path: Path | None = None) -> list[CompanyConfig]:
    """Load company entries using platform, board, and optional careers URL fields."""
    raw = _read_yaml(config_path or PROJECT_ROOT / "config" / "companies.yaml")
    companies = raw.get("companies", [])
    if not isinstance(companies, list):
        raise ValueError("companies.yaml must contain a companies list")
    result: list[CompanyConfig] = []
    for company in companies:
        if not isinstance(company, dict):
            raise ValueError("Each companies.yaml entry must be a mapping")
        name = str(company.get("name", "")).strip()
        platform = str(company.get("platform", "greenhouse")).strip().lower()
        board_value = company.get("board", company.get("greenhouse_board"))
        board = str(board_value).strip() if board_value is not None else None
        url_value = company.get("url")
        url = str(url_value).strip() if url_value is not None else None
        if not name:
            raise ValueError("Each company needs a non-empty name")
        if platform not in {
            "greenhouse",
            "lever",
            "ashby",
            "smartrecruiters",
            "career_page",
        }:
            raise ValueError(f"Unsupported platform {platform!r} for {name}")
        if platform in {"greenhouse", "lever", "ashby", "smartrecruiters"} and not board:
            raise ValueError(f"{platform} company {name} needs a board/company id")
        if platform == "career_page" and not url:
            raise ValueError(f"Career page company {name} needs a url value")
        result.append(CompanyConfig(name=name, platform=platform, board=board, url=url))
    return result
