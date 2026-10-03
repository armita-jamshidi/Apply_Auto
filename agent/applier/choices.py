"""Answer choice questions (radio, checkbox, select, combobox) from saved profile answers.

A question maps to one or more wanted answers. Each wanted answer is a list of acceptable
option texts in priority order, so the same saved fact matches the different wording that
each applicant tracking system uses. Unknown questions return None and stay for manual review.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

Wanted = list[list[str]]
NO_SAVED_ANSWER_NOTE = (
    "No saved answer for this question; answer it in the form, or add it to "
    "application_answers in your profile."
)
CHECKED_SCRIPT = "label => Boolean(label.control && label.control.checked)"
# A searchable dropdown shows its choice outside the input, so read the surrounding control.
COMBOBOX_VALUE_SCRIPT = (
    "element => [element.value || '', "
    "((element.closest('[class*=\"control\"]') || element.parentElement || element)"
    ".innerText || '')].join(' ')"
)
DATE_LABELS = frozenset({"date", "today's date", "todays date", "signature date"})
FORM_TIMEZONE = ZoneInfo("America/New_York")

YES = ["Yes", "I consent", "I agree"]
NO = ["No", "I do not consent", "I don't consent", "I do not agree"]
DECLINE = [
    "I don't wish to answer",
    "I do not wish to answer",
    "Decline to self-identify",
    "Decline to self identify",
    "Prefer not to say",
    "Prefer not to answer",
    "I prefer not to",
    "Decline",
]
GENDER = {
    "female": ["Female", "Woman"],
    "male": ["Male", "Man"],
}
RACE = {
    "middle eastern or north african": [
        "Middle Eastern or North African",
        "Middle Eastern",
        "MENA",
    ],
}
VETERAN_NO = [
    "I am not a protected veteran",
    "not a protected veteran",
    "I am not a veteran",
    "not a veteran",
    "No",
]
DISABILITY_NO = [
    "No, I do not have a disability",
    "No, I don't have a disability",
    "do not have a disability",
    "don't have a disability",
    "No",
]
OTHER = "Other"


def desired_choices(question: str, profile: Mapping[str, Any]) -> Wanted | None:
    """Return the wanted answers for a choice question, or None when nothing is saved."""
    text = _normalize(question)
    answers = _section(profile, "application_answers")
    authorization = _section(profile, "work_authorization")
    eeo = _section(answers, "eeo")

    policy = _policy_answer(text)
    if policy is not None:
        return [YES if policy else NO]
    if re.search(r"(?:meet|satisfy)", text) and re.search(
        r"(?:qualifications?|requirements?|criteria)", text
    ):
        personal = _section(profile, "personal")
        return [YES] if personal.get("meets_job_requirements") is True else None
    if re.search(r"\b(?:authori[sz]ed|eligible|legally able) to work\b", text):
        return _yes_no(authorization.get("authorized_to_work_in_us"))
    if re.search(r"\bsponsor(?:ship)?\b", text):
        return _yes_no(authorization.get("requires_sponsorship"))
    if re.search(r"\bpolygraph\b", text):
        return _yes_no(answers.get("active_polygraph"))
    if re.search(r"\bclearance\b", text):
        if re.search(r"\b(?:eligib\w*|obtain|willing)\b", text):
            return _yes_no(answers.get("eligible_for_security_clearance"))
        return _yes_no(answers.get("active_security_clearance"))
    if re.search(r"\bnote ?-?takers?\b|\btranscri(?:be|ption)\b", text):
        return _yes_no(answers.get("ai_notetaker_consent"))
    if re.search(r"\blanguages?\b", text):
        languages = answers.get("languages")
        if not isinstance(languages, list) or not languages:
            return None
        return [_language_synonyms(str(language)) for language in languages]
    if re.search(r"\bveteran\b", text):
        return _eeo(eeo.get("veteran_status"), lambda value: VETERAN_NO if value == "no" else None)
    if re.search(r"\bdisabilit(?:y|ies)\b", text):
        return _eeo(
            eeo.get("disability_status"), lambda value: DISABILITY_NO if value == "no" else None
        )
    if re.search(r"\bgender\b", text):
        return _eeo(eeo.get("gender"), lambda value: GENDER.get(value, [value]))
    if re.search(r"\brace\b|\bethnicit(?:y|ies)\b", text) and not re.search(
        r"\bhispanic|latin[oax]\b", text
    ):
        return _race(eeo.get("race_ethnicity"))
    return None


def match_options(wanted: Wanted, options: Sequence[str]) -> list[str]:
    """Return the option texts to select, one per wanted answer, in the form's own wording."""
    chosen: list[str] = []
    for synonyms in wanted:
        option = _first_match(synonyms, options)
        if option is not None and option not in chosen:
            chosen.append(option)
    return chosen


def describe(wanted: Wanted) -> str:
    """Short human description of the saved answer, for review notes."""
    return "; ".join(synonyms[0] for synonyms in wanted if synonyms)


def todays_date() -> str:
    """Today's date as forms usually expect it (MM/DD/YYYY), in the candidate's time zone."""
    return datetime.now(FORM_TIMEZONE).strftime("%m/%d/%Y")


@dataclass
class _Group:
    wanted: Wanted | None
    options: list[tuple[str, Locator]] = field(default_factory=list)


class ChoiceGroups:
    """Collect radio/checkbox options per question while scanning labels, then answer them."""

    def __init__(self, profile: Mapping[str, Any]) -> None:
        self._profile = profile
        self._groups: dict[str, _Group] = {}

    def add(
        self, question: str, option_text: str | None = None, label: Locator | None = None
    ) -> None:
        group = self._groups.get(question)
        if group is None:
            group = self._groups[question] = _Group(desired_choices(question, self._profile))
        if option_text is not None and label is not None:
            group.options.append((option_text, label))

    def apply(self, answers: dict[str, str | None], notes: dict[str, str]) -> bool:
        """Tick saved answers; return True when any question still needs manual review."""
        needs_review = False
        for question, group in self._groups.items():
            selected, problem = _select_group(group)
            if problem is None:
                answers[question] = ", ".join(selected)
                continue
            needs_review = True
            answers[question] = None
            notes[question] = problem
        return needs_review


def _select_group(group: _Group) -> tuple[list[str], str | None]:
    if group.wanted is None:
        return [], NO_SAVED_ANSWER_NOTE
    if not group.options:
        return [], "The options for this question could not be read; answer it in the form."
    chosen = match_options(group.wanted, [text for text, _ in group.options])
    if not chosen:
        return [], f"No option matched your saved answer ({describe(group.wanted)})."
    labels = {}
    for text, label in group.options:
        labels.setdefault(text, label)
    selected: list[str] = []
    for text in chosen:
        label = labels[text]
        try:
            if not label.evaluate(CHECKED_SCRIPT):
                label.click()
            if label.evaluate(CHECKED_SCRIPT):
                selected.append(text)
        except PlaywrightError:
            continue
    if len(selected) != len(chosen):
        return selected, f"Could not tick every saved answer ({', '.join(chosen)}); check it."
    return selected, None


def choose_select_option(
    select: Locator, question: str, profile: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """Pick a native <select> option from saved answers; returns (choice, problem note)."""
    wanted = desired_choices(question, profile)
    if wanted is None:
        return None, NO_SAVED_ANSWER_NOTE
    options = [" ".join(text.split()) for text in select.locator("option").all_text_contents()]
    chosen = match_options(wanted, options)
    if len(chosen) != 1:
        return None, f"No option matched your saved answer ({describe(wanted)})."
    select.select_option(label=chosen[0])
    return chosen[0], None


def choose_combobox_option(
    page: Page, combobox: Locator, question: str, profile: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """Pick a searchable-dropdown option from saved answers; returns (choice, problem note)."""
    wanted = desired_choices(question, profile)
    if wanted is None:
        return None, NO_SAVED_ANSWER_NOTE
    try:
        combobox.click()
        options = [" ".join(text.split()) for text in page.get_by_role("option").all_inner_texts()]
        chosen = match_options(wanted, options)
        if len(chosen) != 1:
            combobox.press("Escape")
            return None, f"No option matched your saved answer ({describe(wanted)})."
        page.get_by_role("option", name=chosen[0], exact=True).first.click()
        shown = combobox.evaluate(COMBOBOX_VALUE_SCRIPT) or ""
    except PlaywrightError:
        return None, "The dropdown could not be opened; answer it in the form."
    if chosen[0].casefold() not in " ".join(shown.split()).casefold():
        return None, f"Selected {chosen[0]!r} but could not confirm it; check it."
    return chosen[0], None


def _first_match(synonyms: Sequence[str], options: Sequence[str]) -> str | None:
    normalized = [(_normalize(option), option) for option in options]
    for synonym in synonyms:
        target = _normalize(synonym)
        if not target:
            continue
        for option_text, option in normalized:
            if option_text == target:
                return option
        # Short answers like "Yes"/"No" must start the option, so "No" never matches "Not".
        pattern = rf"^{re.escape(target)}\b" if len(target) <= 3 else rf"\b{re.escape(target)}\b"
        for option_text, option in normalized:
            if re.search(pattern, option_text):
                return option
    return None


def _policy_answer(text: str) -> bool | None:
    asks_prior = re.search(r"\b(?:ever|before|previously|prior|in the past)\b", text)
    if asks_prior and re.search(r"\binterview(?:ed|ing)?\b", text):
        return False
    if asks_prior and re.search(r"\b(?:applied|application)\b", text):
        return False
    if re.search(r"\b(?:ai|artificial intelligence)\b", text) and re.search(
        r"\b(?:policy|policies|guidelines?)\b", text
    ):
        return True
    return None


def _yes_no(value: object) -> Wanted | None:
    if isinstance(value, bool):
        return [YES if value else NO]
    return None


def _eeo(value: object, synonyms_for: Any) -> Wanted | None:
    if value is None:
        return None
    text = _normalize(str(value))
    if text == "decline":
        return [DECLINE]
    synonyms = synonyms_for(text)
    return [synonyms] if synonyms else None


def _race(value: object) -> Wanted | None:
    preferences = value if isinstance(value, list) else [value] if value else []
    if not preferences:
        return None
    if any(_normalize(str(item)) == "decline" for item in preferences):
        return [DECLINE]
    synonyms: list[str] = []
    for item in preferences:
        synonyms.extend(RACE.get(_normalize(str(item)), [str(item)]))
    return [synonyms]


def _language_synonyms(language: str) -> list[str]:
    names = [language, *(part.strip() for part in language.split("/") if part.strip())]
    return [*dict.fromkeys(names), OTHER]


def _section(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else {}


def _normalize(value: str) -> str:
    value = value.replace("’", "'").replace("✱", " ").replace("*", " ")
    return re.sub(r"\s+", " ", value).strip().casefold()
