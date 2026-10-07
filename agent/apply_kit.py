"""job-kit: everything needed to apply to one job, made by two sub-agents.

- The answers sub-agent (agent/answer_agent.py) answers every question on the job's form,
  plus the questions nearly every application asks, for this company and role.
- The resume sub-agent (agent/resume_tailor.py) builds a resume around the job
  description's exact keywords from the resume and the experience bank in profile/library/.

The answers run in the background while the resume sub-agent may ask, in this console, how
you used a keyword your documents never mention. The kit is saved as an attempt of kind
"kit" (answers, drafts, notes, and the resume's path) so the dashboard's Apply panel shows
it, with the Word resume under kits/job-<id>/ (git-ignored). Nothing is submitted.
"""

import argparse
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.answer_agent import KitAnswer, answer_questions, questions_for_job
from agent.resume_format import ResumeSources, load_resume_sources
from agent.settings import PROJECT_ROOT, load_settings
from db.models import Application, Job

LOGGER = logging.getLogger(__name__)
KITS_DIR = PROJECT_ROOT / "kits"
KIT_MODE = "kit"
# Kit note saying the form's questions were read from the job board's API.
BOARD_QUESTIONS_NOTE = "Form questions"
Ask = Callable[[str], str | None]


@dataclass
class Kit:
    """What one job's kit holds."""

    answers: list[KitAnswer] = field(default_factory=list)
    resume_path: Path | None = None
    report_path: Path | None = None
    problems: list[str] = field(default_factory=list)
    from_board: bool = False  # the questions came from the board's API, not a form reading


def kit_folder(job_id: int) -> Path:
    return KITS_DIR / f"job-{job_id}"


def build_kit(
    session: Session,
    job: Job,
    *,
    profile: dict[str, Any],
    resume_text: str,
    client: Any,
    answer_model: str,
    resume_model: str,
    ask: Ask | None = None,
    make_resume: bool = True,
    make_answers: bool = True,
    answerer: Callable[[str, bool], Any] | None = None,
    sources: ResumeSources | None = None,
) -> Kit:
    """Run both sub-agents for one job and record the kit; see the module docstring."""
    from agent.answers import answer_custom_question
    from agent.resume_tailor import keyword_report, output_path, tailor_resume, write_docx

    kit = Kit()
    job_context = {"company": job.company, "title": job.title, "description": job.description}
    recorded = _form_answers(job)
    if not recorded:
        recorded = dict.fromkeys(board_questions(job))
        kit.from_board = bool(recorded)

    def default_answerer(question: str, long_form: bool) -> Any:
        return answer_custom_question(
            question,
            profile,
            resume_text,
            client=client,
            model=answer_model,
            job_context=job_context,
            long_form=long_form,
        )

    def run_answers() -> None:
        try:
            questions = questions_for_job(job.company, job.title, recorded)
            kit.answers = answer_questions(questions, answerer or default_answerer)
        except Exception as error:  # report it with the kit instead of losing the resume
            LOGGER.exception("The answers sub-agent failed")
            kit.problems.append(f"Answers: {error}")

    worker = threading.Thread(target=run_answers, name="answers") if make_answers else None
    if worker is not None:
        worker.start()
    if make_resume:
        try:
            sources = sources or ResumeSources()
            resume = tailor_resume(
                job_context,
                resume_text,
                client=client,
                model=resume_model,
                ask=ask,
                master_cv_text=sources.master_cv_text,
                layout=sources.layout,
            )
            resume.notes = [*sources.notes, *resume.notes]
            folder = kit_folder(job.id)
            kit.resume_path = write_docx(resume, output_path(folder, job.company, job.title))
            kit.report_path = folder / "keywords.md"
            kit.report_path.write_text(keyword_report(resume), encoding="utf-8")
        except Exception as error:
            LOGGER.exception("The resume sub-agent failed")
            kit.problems.append(f"Resume: {error}")
    if worker is not None:
        worker.join()
    record_kit(session, job, kit)
    return kit


def record_kit(session: Session, job: Job, kit: Kit) -> Application:
    """Save the kit as an attempt of kind "kit" so the dashboard shows it."""
    answers = {
        item.question: item.answer if item.kind == "answer" else None for item in kit.answers
    }
    drafts = {item.question: item.answer for item in kit.answers if item.kind == "draft"}
    notes = {item.question: item.note for item in kit.answers if item.note}
    if kit.from_board:
        notes[BOARD_QUESTIONS_NOTE] = "Read from the job board's listing."
    if kit.problems:
        notes["Kit problems"] = "; ".join(kit.problems)
    if kit.report_path is not None and kit.report_path.is_file():
        notes["Resume keywords"] = kit.report_path.read_text(encoding="utf-8")
    application = Application(
        job_id=job.id,
        mode=KIT_MODE,
        answers=answers,
        suggested_answers=drafts or None,
        field_notes=notes or None,
        tailored_resume_path=str(kit.resume_path) if kit.resume_path else None,
        error="; ".join(kit.problems) or None,
    )
    session.add(application)
    session.commit()
    return application


def latest_kit(job: Job) -> Application | None:
    kits = [item for item in job.applications if item.mode == KIT_MODE]
    return max(kits, key=lambda item: (item.started_at, item.id)) if kits else None


def _form_answers(job: Job) -> dict[str, Any]:
    """The questions the job's form asked, from every form attempt (not kits), oldest first.

    One reading can miss questions another found (a later page, an iframe), so all are kept;
    a later attempt's value for the same question wins.
    """
    attempts = sorted(
        (item for item in job.applications if item.mode != KIT_MODE),
        key=lambda item: (item.started_at, item.id),
    )
    recorded: dict[str, Any] = {}
    for attempt in attempts:
        recorded.update(attempt.answers or {})
        for question, draft in (attempt.suggested_answers or {}).items():
            recorded.setdefault(question, None)
            # A draft written before is redone, now in your voice and for this role.
            if recorded.get(question) == draft:
                recorded[question] = None
    return recorded


def board_questions(job: Job) -> list[str]:
    """The job's form questions from its board's public API, where the board publishes them
    (Greenhouse); empty when it does not or the request fails, so the kit still runs."""
    from agent.fetchers.greenhouse import (
        fetch_greenhouse_questions,
        greenhouse_board_token,
        greenhouse_job_id,
    )

    if job.platform != "greenhouse":
        return []
    board, job_id = greenhouse_board_token(job.url), greenhouse_job_id(job.url)
    if not board or not job_id:
        return []
    try:
        return fetch_greenhouse_questions(board, job_id)
    except Exception as error:  # the common questions are still answered
        LOGGER.warning("Could not read the form questions for job %s: %s", job.id, error)
        return []


def form_was_read(job: Job) -> bool:
    """Whether any attempt has read the job's application form."""
    return any(item.mode != KIT_MODE for item in job.applications)


def questions_missing_from_kit(job: Job, kit: Application) -> list[tuple[str, str | None]]:
    """Questions a form reading found that the kit does not answer, such as ones found after
    the kit was made, with any value already on the form."""
    from agent.answer_agent import NOT_QUESTIONS
    from agent.form_agent import _matches_known

    known = [*(kit.answers or {}), *(kit.suggested_answers or {})]
    missing = []
    for question, value in _form_answers(job).items():
        if question in NOT_QUESTIONS or not str(question).strip():
            continue
        if question in known or _matches_known(question, known):
            continue
        missing.append((question, str(value) if value not in (None, "") else None))
    return missing


def ask_in_console(keyword: str) -> str | None:
    """Ask how the candidate used a keyword their documents never mention."""
    print(
        f"\nThe job asks for {keyword!r}, and your resume and experience bank never mention it."
    )
    try:
        answer = input(
            "  How have you used it, and on which project or job? (Enter to skip): "
        ).strip()
    except EOFError:
        return None
    return answer or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare one job's application kit: answers to every question and a resume "
            "tailored to the job description's exact keywords. Nothing is submitted."
        )
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--job-id", type=int, help="The job's id (see --list)")
    target.add_argument("--job-url", help="The job's stored URL")
    target.add_argument(
        "--list", action="store_true", help="List open jobs with their ids and kits, then stop."
    )
    parser.add_argument(
        "--question",
        action="append",
        help="Only answer this question for the job and print it (repeatable; nothing saved).",
    )
    parser.add_argument("--resume", type=Path, help="Defaults to RESUME_PATH")
    parser.add_argument("--profile", type=Path, default=PROJECT_ROOT / "profile" / "profile.yaml")
    parser.add_argument(
        "--no-questions",
        action="store_true",
        help="Do not ask about missing keywords; list them as gaps instead.",
    )
    parser.add_argument("--resume-only", action="store_true", help="Only tailor the resume.")
    parser.add_argument("--answers-only", action="store_true", help="Only answer questions.")
    return parser


def main(argv: list[str] | None = None) -> int:
    from agent.answers import _create_client
    from agent.applier.cli import default_resume_path
    from agent.applier.greenhouse import extract_resume_text, load_profile
    from agent.dashboard import refresh_dashboard
    from db.session import create_database_engine, create_session_factory, ensure_schema

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.list:
        return _list_jobs(settings.database_url)
    profile = load_profile(args.profile)
    resume_text = extract_resume_text(args.resume or default_resume_path())
    sources = load_resume_sources()
    engine = create_database_engine(settings.database_url)
    try:
        ensure_schema(engine)
        with create_session_factory(engine)() as session:
            query = (
                select(Job).where(Job.id == args.job_id)
                if args.job_id is not None
                else select(Job).where(Job.url == args.job_url)
            )
            job = session.scalar(query)
            if job is None:
                print("No such job. Use the id shown in the dashboard's Details.")
                return 1
            if args.question:
                return _answer_only(job, args.question, profile, resume_text, settings)
            print(f"Preparing the kit for {job.company} - {job.title}")
            kit = build_kit(
                session,
                job,
                profile=profile,
                resume_text=resume_text,
                client=_create_client(),
                answer_model=settings.anthropic_model,
                resume_model=settings.resume_tailor_model,
                ask=None if args.no_questions else ask_in_console,
                make_resume=not args.answers_only,
                sources=sources,
                make_answers=not args.resume_only,
            )
    finally:
        engine.dispose()
    refresh_dashboard()
    ready = sum(item.answer is not None for item in kit.answers)
    print(f"\nAnswered {ready} of {len(kit.answers)} questions.")
    if kit.resume_path:
        print(f"Tailored resume: {kit.resume_path}")
    if kit.report_path:
        print(kit.report_path.read_text(encoding="utf-8"))
    for problem in kit.problems:
        print(f"Problem: {problem}")
    print("Open the dashboard's Apply panel for this job to see everything.")
    return 0 if not kit.problems else 2


def _list_jobs(database_url: str) -> int:
    from sqlalchemy.orm import selectinload

    from db.session import create_database_engine, create_session_factory, ensure_schema

    engine = create_database_engine(database_url)
    try:
        ensure_schema(engine)
        with create_session_factory(engine)() as session:
            jobs = session.scalars(
                select(Job)
                .options(selectinload(Job.applications))
                .where(Job.status.in_(("new", "queued", "manual_review")))
                .order_by(Job.fit_score.desc().nulls_last(), Job.id)
            ).all()
            for job in jobs:
                kit = latest_kit(job)
                made = f"kit {kit.started_at:%Y-%m-%d}" if kit is not None else "no kit"
                fit = job.fit_score if job.fit_score is not None else "-"
                print(f"{job.id:>5} | fit {fit:>3} | {made:<14} | {job.company} | {job.title}")
    finally:
        engine.dispose()
    return 0


def _answer_only(
    job: Job, questions: list[str], profile: dict[str, Any], resume_text: str, settings: Any
) -> int:
    from agent.answers import _create_client, answer_custom_question

    client = _create_client()
    context = {"company": job.company, "title": job.title, "description": job.description}
    answers = answer_questions(
        [(question, None) for question in questions],
        lambda question, long_form: answer_custom_question(
            question,
            profile,
            resume_text,
            client=client,
            model=settings.anthropic_model,
            job_context=context,
            long_form=long_form,
        ),
    )
    for item in answers:
        print(f"\nQ: {item.question}\n[{item.kind}] {item.answer or '(no answer)'}")
        if item.note:
            print(f"Note: {item.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
