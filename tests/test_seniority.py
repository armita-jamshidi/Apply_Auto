"""Tests for classifying a job's experience level."""

import pytest

from agent.seniority import classify_experience, required_years


@pytest.mark.parametrize(
    ("title", "description", "level"),
    [
        ("Software Engineer, New Grad (2027)", "", "early"),
        ("Software Engineering Intern - Summer 2027", "", "early"),
        ("Junior Data Analyst", "", "early"),
        ("Associate Product Manager", "", "early"),
        ("Software Engineer I", "", "early"),
        ("Software Engineer", "0-2 years of experience building web services.", "early"),
        ("Software Engineer", "1+ years of professional experience with Python.", "early"),
        ("Senior Software Engineer", "", "senior"),
        ("Staff Engineer, Platform", "", "senior"),
        ("Engineering Manager", "", "senior"),
        ("Software Engineer", "5+ years of experience in distributed systems.", "senior"),
        ("Software Engineer II", "", "mid"),
        ("Backend Engineer", "3+ years of experience with Go.", "mid"),
        ("Software Engineer", "You will build tools for our customers.", "unknown"),
        ("Associate Software Engineer", "Requires 6+ years of experience.", "senior"),
    ],
)
def test_classify_experience(title: str, description: str, level: str) -> None:
    assert classify_experience(title, description).level == level


@pytest.mark.parametrize(
    ("description", "years"),
    [
        ("At least 3 years of experience with React.", 3),
        ("2-4 years of experience; 5+ years of experience with Kubernetes preferred.", 2),
        ("4+ years experience in Python. 2+ years experience with AWS.", 4),
        ("Experience with SQL is a plus. Founded 10 years ago.", None),
        ("Minimum of 1 year of industry experience.", 1),
        ("• 7 years of experience leading teams", 7),
    ],
)
def test_required_years(description: str, years: int | None) -> None:
    assert required_years(description) == years


def test_assessment_explains_its_decision() -> None:
    assessment = classify_experience("Backend Engineer", "Requires 3+ years of experience.")

    assert (assessment.level, assessment.min_years) == ("mid", 3)
    assert "3+ years" in assessment.reason


@pytest.mark.parametrize(
    ("description", "years"),
    [
        # Bullets that never say "experience" still state a requirement.
        ("<ul><li>8+ years building data platforms</li></ul>", 8),
        ("Qualifications: 8+ years in data and analytics roles", 8),
        ("You have 8+ years working with data &amp; AI systems", 8),
        ("Experience:\n8+ years", 8),
        ("8 to 10 years in software engineering", 8),
        ("Eight or more years of experience in AI", 8),
        ("Eight (8) years of experience in AI", 8),
        ("8+ yrs experience", 8),
        ("BS with 8+ years of experience preferred; MS with 5+ years required", 5),
        ("1-2 years of experience with Python", 1),
        # Numbers of years that are not experience requirements.
        ("We have been in business for 20 years.", None),
        ("Founded 25 years ago, we build tools.", None),
        ("A 4-year degree in computer science.", None),
        ("Our product has served customers for over 10 years.", None),
        ("Offer valid for 2 years.", None),
    ],
)
def test_required_years_reads_common_phrasings(description: str, years: int | None) -> None:
    assert required_years(description) == years
