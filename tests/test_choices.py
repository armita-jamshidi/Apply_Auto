"""Tests for answering choice questions from saved profile answers (fictional values)."""

import pytest

from agent.applier.choices import DECLINE, desired_choices, match_options, saved_text_answer

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


LOCATION_PROFILE = {
    "application_answers": {
        "current_company": "Example Bakery",
        "how_did_you_hear": "Online",
        "earliest_start_date": "11/30/2026",
        "current_location": "Durham, North Carolina",
        "currently_in_us": True,
        "willing_to_relocate": False,
        "onsite_ok_in": ["North Carolina"],
        "max_onsite_days_per_week": 5,
        "eeo": {"hispanic_latino": "no", "lgbtq": "no"},
    }
}


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Are you currently based in or willing to relocate to the Bay Area?", ["No"]),
        ("Are you willing to relocate to Raleigh, NC?", ["Yes"]),
        ("Are you currently located in the US? *", ["Yes"]),
        ("Are you able to work from our US office three days per week?", ["No"]),
        ("Are you willing to work on-site 5 days a week in Durham, NC?", ["Yes"]),
        ("Can you work on-site 6 days a week in Charlotte, North Carolina?", ["No"]),
        ("This role is hybrid in Research Triangle Park. Are you able to commute?", ["Yes"]),
        ("Are you Hispanic or Latino?", ["No"]),
        ("I consider myself a member of the LGBTQ+ community. (optional)", ["No"]),
    ],
)
def test_location_and_identity_answers(question: str, expected: list[str]) -> None:
    assert choose(question, ["Yes", "No"], LOCATION_PROFILE) == expected


def test_hispanic_option_wording_is_matched() -> None:
    options = ["Hispanic or Latino", "Not Hispanic or Latino", "Decline to self-identify"]

    assert choose("Ethnicity: Hispanic/Latino", options, LOCATION_PROFILE) == [
        "Not Hispanic or Latino"
    ]


def test_how_did_you_hear_dropdown_falls_back_to_other() -> None:
    assert choose(
        "How did you hear about this job?", ["LinkedIn", "Referral", "Other"], LOCATION_PROFILE
    ) == ["Other"]
    assert choose(
        "How did you hear about us?", ["Online job board", "Referral"], LOCATION_PROFILE
    ) == ["Online job board"]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How did you hear about this job?", "Online"),
        ("When can you start a new role?", "11/30/2026"),
        ("Earliest start date *", "11/30/2026"),
        ("Current company", "Example Bakery"),
        ("Current location \u2731", "Durham, North Carolina"),
        ("Location (City) *", "Durham, North Carolina"),
        ("Why do you want to work here?", None),
    ],
)
def test_saved_text_answers(question: str, expected: str | None) -> None:
    assert saved_text_answer(question, LOCATION_PROFILE) == expected


def test_onsite_questions_stay_manual_without_saved_places() -> None:
    assert desired_choices("Can you work on-site in Austin, TX?", PROFILE) is None
