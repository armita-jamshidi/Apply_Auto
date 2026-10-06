"""Tests for the pre-commit personal-data guard (fictional profile values only)."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_private_data.py"
spec = importlib.util.spec_from_file_location("check_private_data", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

PROFILE = {
    "personal": {
        "name": "Jordan Example",
        "email": "jordan.example@example.com",
        "phone": "+1-555-010-2048",
        "linkedin": "https://www.linkedin.com/in/jordan-example",
        "github": "https://github.com/jordan-example",
        "location": "Raleigh, NC",
    },
    "application_answers": {
        "current_company": "Example Bakery",
        "earliest_start_date": "01/15/2027",
    },
    "experience": [{"company": "Sample Systems"}],
    "education": [{"institution": "Example State University"}],
}


def test_private_files_are_blocked_but_examples_are_allowed() -> None:
    staged = [
        "profile/profile.yaml",
        "profile/resume_Example_2026.pdf",
        ".env",
        "data/job_agent.db",
        "reviews/lever-x.html",
        "screenshots/shot.png",
        "dashboard.html",
        ".playwright/profile/Default/Cookies",
        ".env.example",
        "profile/profile.example.yaml",
        "agent/dashboard.py",
    ]

    assert guard.private_paths(staged) == staged[:8]


def test_workspace_personal_context_is_blocked_but_templates_are_allowed() -> None:
    staged = [
        "context/private/about-me.md",
        "customers/candidate.md",
        "context/product.md",
        "customers/README.md",
        "customers/candidate.example.md",
    ]

    assert guard.private_paths(staged) == staged[:2]


def test_personal_values_cover_identity_links_employers_and_resume() -> None:
    values = guard.personal_values(PROFILE, "profile/resume_Example_2026.pdf")

    assert values["personal.name part 2"] == "Example"
    assert values["personal.github handle"] == "jordan-example"
    assert values["personal.email user"] == "jordan.example"
    assert values["application_answers.current_company"] == "Example Bakery"
    assert values["experience[0].company"] == "Sample Systems"
    assert values["education[0].institution"] == "Example State University"
    assert values["resume file name"] == "resume_Example_2026.pdf"


def test_added_lines_with_personal_details_are_reported_by_category() -> None:
    values = guard.personal_values(PROFILE)
    lines = [
        'assert page.fields["Phone"].value == "(555) 010-2048"',
        "url = 'https://github.com/jordan-example/project'",
        "Worked at Example Bakery.",
    ]

    found = guard.find_personal_details(lines, values)

    assert "personal.phone" in found
    assert "personal.github" in found or "personal.github handle" in found
    assert "application_answers.current_company" in found


def test_values_must_match_whole_words() -> None:
    values = guard.personal_values(PROFILE)

    assert guard.find_personal_details(["def jordanify(): pass  # Examples"], values) == []


def test_clean_changes_pass() -> None:
    values = guard.personal_values(PROFILE)

    assert guard.find_personal_details(["def classify(title): return 'early'"], values) == []
