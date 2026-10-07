"""Classify how much experience a job expects, to find early-career roles."""

import re
from dataclasses import dataclass
from html import unescape
from typing import Literal

ExperienceLevel = Literal["early", "mid", "senior", "unknown"]
EARLY_MAX_YEARS = 2
SENIOR_MIN_YEARS = 5

_SENIOR_TITLE = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|lead|manager|director|head of|vp|vice president"
    r"|chief|architect|distinguished|fellow engineer)\b",
    re.IGNORECASE,
)
_EARLY_TITLE = re.compile(
    r"\b(?:intern(?:ship)?|co-?op|new grad(?:uate)?|recent grad(?:uate)?|graduate|university"
    r"|campus|entry[- ]level|junior|jr\.?|associate|early[- ]career|apprentice(?:ship)?"
    r"|rotational|residency|20\d\d grad)\b",
    re.IGNORECASE,
)
# "Engineer I" / "Analyst 1" mark the first level; II and up are later levels.
_LEVEL_ONE = re.compile(r"\b(?:I|1)\s*(?:$|[,(\-–/])", re.IGNORECASE)
_LEVEL_LATER = re.compile(r"\b(?:II|III|IV|V|2|3|4|5)\s*(?:$|[,(\-–/])")
_YEARS = re.compile(
    r"(?:(?P<floor>at least|minimum of|min\.?)|over)?\s*(\d{1,2})\s*(?P<plus>\+|plus|or more)?\s*"
    r"(?:(?:-|–|to)\s*(\d{1,2})\s*)?(?P<plus2>\+)?\s*(?:years?|yrs?)\b"
    r"(?P<after>[\s'’]*(?:of\s+)?\w*)",
    re.IGNORECASE,
)
# Words that follow a requirement such as "8+ years building data platforms".
_REQUIREMENT_AFTER = re.compile(
    r"^[\s'’]*(?:of\s+)?(?:experience|experienced|professional|industry|relevant|related"
    r"|hands|work|working|building|developing|designing|leading|delivering|deploying"
    r"|shipping|managing|writing|programming|coding|in|with|as|across|doing|applying"
    r"|production|software|engineering|data|machine|ml|ai)\b",
    re.IGNORECASE,
)
# "in business for 20 years" or "a 4-year degree" are not experience requirements.
_NOT_REQUIREMENT_AFTER = re.compile(
    r"^[\s'’]*(?:of\s+)?(?:ago|old|history|existence|in business|business|degree|program)\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    word: str(number)
    for number, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen".split()
    )
}
# "eight (8) or more years" and "Eight+ years" become "8 or more years" and "8+ years".
_SPELLED_YEARS = re.compile(
    r"\b(" + "|".join(_NUMBER_WORDS) + r")\b(?:\s*\(\d{1,2}\))?"
    r"(?=\s*(?:\+|plus|or more)?\s*(?:-|–|to)?\s*\w*\s*(?:years?|yrs?)\b)",
    re.IGNORECASE,
)
_PREFERRED = re.compile(
    r"\b(?:preferred|nice to have|bonus|a plus|ideally|desired)\b", re.IGNORECASE
)
_EXPERIENCE = re.compile(r"\bexperience\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExperienceAssessment:
    """Experience level for a job, the minimum years it requires, and why."""

    level: ExperienceLevel
    min_years: int | None
    reason: str


def required_years(description: str) -> int | None:
    """Return the highest minimum years of experience a description requires."""
    minimums: list[int] = []
    description = _plain_text(description)
    description = _SPELLED_YEARS.sub(lambda m: _NUMBER_WORDS[m.group(1).casefold()], description)
    for sentence in re.split(r"(?<=[.!?;\n])\s*|\s+-\s+|•", description):
        if _PREFERRED.search(sentence):
            continue
        mentions_experience = bool(_EXPERIENCE.search(sentence))
        for match in _YEARS.finditer(sentence):
            years = int(match.group(2))
            if years > 30 or _NOT_REQUIREMENT_AFTER.match(match.group("after")):
                continue
            # A bare "8+ years" line (a bullet under "Experience:") still states a minimum.
            stated_minimum = any(match.group(name) for name in ("floor", "plus", "plus2"))
            if (
                mentions_experience
                or stated_minimum
                or _REQUIREMENT_AFTER.match(match.group("after"))
            ):
                minimums.append(years)
    return max(minimums) if minimums else None


def _plain_text(text: str) -> str:
    # Greenhouse descriptions arrive as escaped HTML; keep list items as separate lines.
    text = unescape(unescape(text))
    text = re.sub(r"<\s*(?:br|/p|/li|/div|/h\d)\s*/?>", "\n", text, flags=re.IGNORECASE)
    return re.sub(r"<[^>]+>", " ", text)


def classify_experience(title: str, description: str = "") -> ExperienceAssessment:
    """Decide whether a job is early-career from its title, then its required years."""
    title_text = " ".join(title.split())
    years = required_years(description)
    senior_words = {match.group(0).casefold() for match in _SENIOR_TITLE.finditer(title_text)}
    # "Associate Product Manager" is an entry role: "manager" there is the job, not the level.
    if senior_words == {"manager"} and _EARLY_TITLE.search(title_text):
        senior_words = set()
    if senior_words:
        return ExperienceAssessment("senior", years, "title names a senior level")
    if years is not None and years >= SENIOR_MIN_YEARS:
        return ExperienceAssessment("senior", years, f"requires {years}+ years")
    if _EARLY_TITLE.search(title_text):
        return ExperienceAssessment("early", years, "title names an early-career level")
    if _LEVEL_LATER.search(title_text):
        return ExperienceAssessment("mid", years, "title names a later level")
    if years is not None and years > EARLY_MAX_YEARS:
        return ExperienceAssessment("mid", years, f"requires {years}+ years")
    if _LEVEL_ONE.search(title_text):
        return ExperienceAssessment("early", years, "title names the first level")
    if years is not None:
        return ExperienceAssessment("early", years, f"requires {years} years or fewer")
    return ExperienceAssessment("unknown", None, "no experience level stated")
