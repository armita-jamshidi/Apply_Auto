"""The answers sub-agent: every application question for one job, answered for that job.

The questions come from the job's application form (recorded when its form was read by a
board filler or the form agent), plus the questions nearly every application asks (why this
company, why this role, a technical project you are proud of), so the Apply panel always has
them ready even before the form has been read.

Each question goes through the grounded answer engine with this job's company, title, and
description: short factual questions get exact phrases from the profile and resume, and
written questions get drafts from the source library, cited sentence by sentence and written
in the candidate's voice when samples of their writing are in profile/writing_samples/.
Answers already filled from the profile on the form are kept as they are.
"""

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from agent.answers import AnswerDecision, _is_open_ended

LOGGER = logging.getLogger(__name__)
COMMON_QUESTIONS = (
    "Why do you want to work at {company}?",
    "Why are you a good fit for the {title} role?",
    "Tell us about a technical project you are proud of.",
)
# Notes the form fillers keep that are not questions on the form.
NOT_QUESTIONS = frozenset({"Agent summary", "Resume", "Needed the candidate"})
Answerer = Callable[[str, bool], AnswerDecision]


@dataclass(frozen=True, slots=True)
class KitAnswer:
    """One question and what to put in it: an answer, a draft to review, or a note."""

    question: str
    answer: str | None
    kind: str  # "answer", "draft", or "needs_you"
    note: str = ""


def questions_for_job(
    company: str, title: str, recorded: Mapping[str, Any] | None = None
) -> list[tuple[str, str | None]]:
    """(question, answer already on the form) for the job: its form's questions, then the
    common ones its form did not already ask."""
    from agent.form_agent import _matches_known

    questions: list[tuple[str, str | None]] = []
    for question, value in (recorded or {}).items():
        if question in NOT_QUESTIONS or not str(question).strip():
            continue
        questions.append((str(question), str(value) if value not in (None, "") else None))
    known = [question for question, _ in questions]
    for template in COMMON_QUESTIONS:
        question = template.format(company=company or "this company", title=title or "this")
        if not _matches_known(question, known) and not _similar_prompt(question, known):
            questions.append((question, None))
    return questions


def answer_questions(
    questions: list[tuple[str, str | None]], answerer: Answerer, *, workers: int = 4
) -> list[KitAnswer]:
    """Answer every question that has no answer yet, in parallel; keep the form's answers."""

    def answer(item: tuple[str, str | None]) -> KitAnswer:
        question, existing = item
        if existing:
            return KitAnswer(question, existing, "answer", "Filled from your profile on the form.")
        try:
            decision = answerer(question, _is_written(question))
        except Exception as error:  # one failed question must not lose the others
            LOGGER.warning("Could not answer %r: %s", question, error)
            return KitAnswer(question, None, "needs_you", f"Could not get an answer: {error}")
        if decision.answer is None:
            return KitAnswer(question, None, "needs_you", decision.reason or "No source for this.")
        if decision.needs_manual_review:
            return KitAnswer(
                question, decision.answer, "draft", decision.reason or "Draft: review it."
            )
        return KitAnswer(question, decision.answer, "answer", decision.evidence or "")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(answer, questions))


def _is_written(question: str) -> bool:
    """Whether a question wants written prose (a draft) rather than a short fact."""
    lowered = question.casefold()
    return (
        _is_open_ended(question)
        or len(question) > 90
        or any(word in lowered for word in ("why ", "tell us", "describe", "proud", "explain"))
    )


def _similar_prompt(question: str, known: list[str]) -> bool:
    """Whether the form already asks the same kind of question in other words."""
    lowered = question.casefold()
    topics = (
        ("why do you want", ("why do you want", "why are you interested", "why us", "why join")),
        ("good fit", ("good fit", "why should we hire", "qualif", "why are you a")),
        ("technical project", ("project", "accomplishment", "proud")),
    )
    for marker, signals in topics:
        if marker in lowered:
            return any(signal in item.casefold() for item in known for signal in signals)
    return False
