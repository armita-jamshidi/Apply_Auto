"""Load local YAML configuration and environment overrides."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class CompanyConfig:
    """A company entry from config/companies.yaml."""

    name: str
    platform: str
    board: str | None
    url: str | None


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
    )


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
