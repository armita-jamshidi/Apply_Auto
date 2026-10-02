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
