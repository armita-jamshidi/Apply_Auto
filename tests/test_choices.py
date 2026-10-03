"""Tests for answering choice questions from saved profile answers (fictional values)."""

import pytest

from agent.applier.choices import DECLINE, desired_choices, match_options

PROFILE = {
    "work_authorization": {"authorized_to_work_in_us": True, "requires_sponsorship": False},
    "application_answers": {
        "active_security_clearance": False,
        "eligible_for_security_clearance": True,
        "ai_notetaker_consent": True,
        "languages": ["English", "Farsi/Persian"],
        "eeo": {
            "gender": "female",
            "race_ethnicity": ["Middle Eastern or North African", "White"],
            "veteran_status": "no",
            "disability_status": "no",
        },
    },
}


def choose(question: str, options: list[str], profile: dict = PROFILE) -> list[str]:
    wanted = desired_choices(question, profile)
    assert wanted is not None, question
    return match_options(wanted, options)


@pytest.mark.parametrize(
    ("question", "options", "expected"),
    [
        (
            "Are you legally authorized to work in the country for which you are applying? ✱",
            ["Yes", "No"],
            ["Yes"],
        ),
        (
            "Will you now or in the future require sponsorship for employment visa status?",
            ["Yes", "No"],
            ["No"],
        ),
        ("Do you currently hold an active US security clearance? ✱", ["Yes", "No"], ["No"]),
        (
            "Are you eligible to obtain the security clearance specified in the job description?",
            ["Yes", "No"],
            ["Yes"],
        ),
        (
            "As part of our interview process, we may use AI notetakers to transcribe interviews.",
            ["Yes, I consent", "No, I do not consent"],
            ["Yes, I consent"],
        ),
        ("Have you ever interviewed with us before?", ["Yes", "No"], ["No"]),
    ],
)
def test_yes_no_questions_use_saved_facts(
    question: str, options: list[str], expected: list[str]
) -> None:
    assert choose(question, options) == expected


def test_languages_check_each_language_and_fall_back_to_other() -> None:
    options = ["English (ENG)", "Spanish (SPA)", "French (FRA)", "Other", "Choose not to disclose"]

    assert choose("Language Skill(s) (Check all that apply) ✱", options) == [
        "English (ENG)",
        "Other",
    ]


def test_language_named_option_is_preferred_over_other() -> None:
    assert choose("Languages spoken", ["English", "Persian", "Other"]) == ["English", "Persian"]


def test_race_takes_first_offered_preference() -> None:
    with_mena = ["Asian", "Middle Eastern or North African", "White", "Decline to self-identify"]
    without_mena = ["Asian", "Hispanic or Latino", "White (Not Hispanic or Latino)", "Decline"]

    assert choose("Race", with_mena) == ["Middle Eastern or North African"]
    assert choose("Race and Ethnicity *", without_mena) == ["White (Not Hispanic or Latino)"]


def test_hispanic_question_is_not_answered_from_race_preferences() -> None:
    assert desired_choices("Are you Hispanic or Latino?", PROFILE) is None


@pytest.mark.parametrize(
    ("question", "options", "expected"),
    [
        ("Gender", ["Male", "Female", "Decline to self-identify"], ["Female"]),
        ("Gender Identity (optional)", ["Man", "Woman", "Non-binary"], ["Woman"]),
        (
            "Veteran Status *",
            [
                "I am a protected veteran",
                "I am not a protected veteran",
                "I don't wish to answer",
            ],
            ["I am not a protected veteran"],
        ),
        (
            "Disability status",
            [
                "Yes, I have a disability (or previously had a disability)",
                "No, I don’t have a disability",
                "I don't wish to answer",
            ],
            ["No, I don’t have a disability"],
        ),
    ],
)
def test_demographic_answers_match_each_forms_wording(
    question: str, options: list[str], expected: list[str]
) -> None:
    assert choose(question, options) == expected


def test_no_never_matches_options_that_merely_start_with_not() -> None:
    assert match_options([["No"]], ["Not sure", "Nope", "No, never"]) == ["No, never"]


def test_decline_setting_picks_the_forms_decline_option() -> None:
    profile = {"application_answers": {"eeo": {"gender": "decline"}}}

    assert desired_choices("Gender", profile) == [DECLINE]
    assert choose("Gender", ["Female", "Male", "I don't wish to answer"], profile) == [
        "I don't wish to answer"
    ]


@pytest.mark.parametrize(
    "question",
    ["How did you hear about this job?", "I consider myself a member of the LGBTQ+ community."],
)
def test_unsaved_questions_stay_manual(question: str) -> None:
    assert desired_choices(question, PROFILE) is None


def test_missing_profile_facts_stay_manual() -> None:
    assert desired_choices("Do you require visa sponsorship?", {}) is None


@pytest.mark.parametrize(
    "question",
    [
        "Applicant Arbitration Agreement Acknowledgement",
        "I acknowledge that I have opened, read, and understood the Arbitration Agreement.",
        "I hereby certify that I have not knowingly withheld any information.",
    ],
)
def test_legal_acknowledgements_are_never_answered_automatically(question: str) -> None:
    assert desired_choices(question, PROFILE) is None
