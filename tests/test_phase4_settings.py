"""Configuration tests for Tier 1 ATS routing and live rate limits."""

from pathlib import Path

from agent.settings import load_companies, load_settings


def test_companies_config_accepts_all_tier1_platforms(tmp_path: Path) -> None:
    companies_path = tmp_path / "companies.yaml"
    companies_path.write_text(
        "companies:\n"
        "  - name: Green Example\n    platform: greenhouse\n    board: green-example\n"
        "  - name: Lever Example\n    platform: lever\n    board: lever-example\n"
        "  - name: Ashby Example\n    platform: ashby\n    board: ashby-example\n"
        "  - name: Smart Example\n    platform: smartrecruiters\n    board: smart-example\n",
        encoding="utf-8",
    )

    companies = load_companies(companies_path)

    assert [company.platform for company in companies] == [
        "greenhouse",
        "lever",
        "ashby",
        "smartrecruiters",
    ]


def test_settings_load_live_application_caps(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "database_url: 'sqlite+pysqlite:///:memory:'\n"
        "fit_score_threshold: 75\n"
        "safeguards:\n"
        "  daily_application_cap: 3\n"
        "  company_monthly_application_cap: 2\n",
        encoding="utf-8",
    )

    settings = load_settings(settings_path, load_env=False)

    assert settings.fit_score_threshold == 75
    assert settings.daily_application_cap == 3
    assert settings.company_monthly_application_cap == 2


def test_relative_sqlite_file_is_anchored_to_the_project(monkeypatch, tmp_path: Path) -> None:
    from agent import settings

    monkeypatch.setattr(settings, "PROJECT_ROOT", tmp_path)

    resolved = settings.resolve_database_url("sqlite:///data/jobs.db")

    assert resolved == f"sqlite:///{(tmp_path / 'data' / 'jobs.db').as_posix()}"
    assert (tmp_path / "data").is_dir()


def test_non_file_database_urls_are_unchanged() -> None:
    from agent.settings import resolve_database_url

    postgres = "postgresql+psycopg://user:pw@localhost:5432/jobs"
    assert resolve_database_url(postgres) == postgres
    assert resolve_database_url("sqlite+pysqlite:///:memory:") == "sqlite+pysqlite:///:memory:"


def test_discovery_experience_levels_default_and_validation(tmp_path: Path) -> None:
    import pytest

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("database_url: 'sqlite+pysqlite:///:memory:'\n", encoding="utf-8")
    assert load_settings(settings_path, load_env=False).experience_levels == ("early", "unknown")

    settings_path.write_text(
        "database_url: 'sqlite+pysqlite:///:memory:'\n"
        "discovery:\n  experience_levels: [Early, MID]\n",
        encoding="utf-8",
    )
    assert load_settings(settings_path, load_env=False).experience_levels == ("early", "mid")

    settings_path.write_text(
        "database_url: 'sqlite+pysqlite:///:memory:'\n"
        "discovery:\n  experience_levels: [junior]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="experience_levels"):
        load_settings(settings_path, load_env=False)


def test_title_keywords_filter_roles_by_whole_words(tmp_path: Path) -> None:
    from agent.settings import title_matches

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "database_url: 'sqlite+pysqlite:///:memory:'\n"
        "discovery:\n  title_keywords: [Engineer, AI, machine learning]\n",
        encoding="utf-8",
    )
    keywords = load_settings(settings_path, load_env=False).title_keywords

    assert keywords == ("engineer", "ai", "machine learning")
    assert title_matches("Software Engineer, New Grad", keywords)
    assert title_matches("Machine Learning Intern", keywords)
    assert title_matches("AI Researcher", keywords)
    assert not title_matches("Enterprise Account Executive", keywords)
    assert not title_matches("Retail Associate (Maintenance)", keywords)
    assert title_matches("Anything at all", ())
