"""Load local YAML configuration and environment overrides."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

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
    anthropic_model = str(raw.get("anthropic_model", "claude-sonnet-4-20250514"))
    anthropic_model = str(raw.get("anthropic_model", "claude-sonnet-5-5"))
    if not database_url:
        raise ValueError("Set database_url in settings.yaml or DATABASE_URL in the environment")
    if not 0 <= fit_score_threshold <= 100:
        raise ValueError("fit_score_threshold must be between 0 and 100")
    return AgentSettings(
        database_url=database_url,
        include_hybrid_nc=bool(location.get("include_hybrid_nc", True)),
        max_retries=int(network.get("max_retries", 3)),
        backoff_seconds=float(network.get("backoff_seconds", 0.5)),
        fit_score_threshold=fit_score_threshold,
        anthropic_model=anthropic_model,
    )


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
        if platform not in {"greenhouse", "lever", "ashby", "career_page"}:
            raise ValueError(f"Unsupported platform {platform!r} for {name}")
        if platform == "greenhouse" and not board:
            raise ValueError(f"Greenhouse company {name} needs a board value")
        if platform == "career_page" and not url:
            raise ValueError(f"Career page company {name} needs a url value")
        result.append(CompanyConfig(name=name, platform=platform, board=board, url=url))
    return result
