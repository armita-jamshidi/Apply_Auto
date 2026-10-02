"""Configuration tests for Tier 1 ATS routing and live rate limits."""

from pathlib import Path

from agent.settings import load_companies


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
