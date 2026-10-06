"""The apply kit: a keyword-tailored resume and every question answered, all grounded."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import apply_kit, dashboard
from agent.answer_agent import KitAnswer, answer_questions, questions_for_job
from agent.answers import AnswerDecision
from agent.library import LibraryDocument
from agent.resume_tailor import (
    KEYWORD_PROMPT,
    POOL_PROMPT,
    Keywords,
    bullet_problem,
    extract_keywords,
    tailor_resume,
    write_docx,
)
from db.models import Application, Base, Job

RESUME = """Sam Sample
sam@example.com | github.com/sam
Example University, B.S. Computer Science, 2025
Research Assistant, Example Lab
Built a retrieval pipeline in Python that answered 120 questions a day."""
BANK = """# Projects
## Agent Evaluator
Wrote an evaluation harness in Python for LLM agents, scoring 40 tasks.
Used PyTorch to fine-tune a small ranking model."""
DESCRIPTION = (
    "<p>You will <b>evaluate</b> LLM agents and collaborate with researchers. "
    "Experience with Python, PyTorch, and Kubernetes. Build evaluation pipelines.</p>"
)
JOB = {"company": "Acme", "title": "AI Engineer", "description": DESCRIPTION}


class FakeClient:
    """Answers each structured request from a script keyed by its system prompt."""

    def __init__(self, tailor_replies: list[dict]) -> None:
        self.tailor_replies = list(tailor_replies)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        if request["system"] == KEYWORD_PROMPT:
            payload = {
                "action_verbs": ["Evaluate", "collaborate", "lead"],  # "lead" is not there
                "technologies": ["python", "PyTorch", "Kubernetes"],
                "concepts": ["evaluation pipelines"],
            }
        elif request["system"] == POOL_PROMPT:
            payload = {
                "name": "Sam Sample",
                "contact": ["sam@example.com", "github.com/sam"],
                "education": ["Example University, B.S. Computer Science, 2025"],
                "entries": [
                    {
                        "title": "Research Assistant", "organization": "Example Lab",
                        "location": "", "dates": "", "kind": "experience", "doc_id": "resume",
                        "facts": ["Built a retrieval pipeline in Python that answered 120 "
                                  "questions a day."],
                    },
                    {
                        "title": "Agent Evaluator", "organization": "", "location": "",
                        "dates": "", "kind": "project", "doc_id": "bank.md",
                        "facts": ["Wrote an evaluation harness in Python for LLM agents, "
                                  "scoring 40 tasks."],
                    },
                    {
                        "title": "Imaginary Startup", "organization": "", "location": "",
                        "dates": "", "kind": "experience", "doc_id": "resume", "facts": [],
                    },
                ],
            }
        else:
            payload = self.tailor_replies.pop(0)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )


def _bullet(text: str, *quotes: tuple[str, str]) -> dict:
    return {"text": text, "evidence": [{"doc_id": d, "quote": q} for d, q in quotes]}


GOOD = _bullet(
    "Evaluate LLM agents with an evaluation harness in Python, scoring 40 tasks",
    ("bank.md", "Wrote an evaluation harness in Python for LLM agents, scoring 40 tasks."),
)
CLAIMS_K8S = _bullet(
    "Deployed the retrieval pipeline on Kubernetes",
    ("resume", "Built a retrieval pipeline in Python"),
)


def test_keywords_are_kept_only_as_the_description_spells_them() -> None:
    keywords = extract_keywords(
        " ".join(DESCRIPTION.split()), client=FakeClient([]), model="m"
    )

    assert keywords.action_verbs == ["evaluate", "collaborate"]
    assert keywords.technologies == ["Python", "PyTorch", "Kubernetes"]
    assert keywords.concepts == ["evaluation pipelines"]


def test_bullets_naming_unsupported_tools_or_numbers_are_rejected() -> None:
    docs = {"resume": LibraryDocument("resume", "Resume", RESUME)}
    keywords = Keywords(technologies=["Python", "Kubernetes"])

    assert "Kubernetes" in bullet_problem(CLAIMS_K8S, keywords, docs)
    inflated = _bullet(
        "Built a retrieval pipeline in Python serving 500 questions a day",
        ("resume", "Built a retrieval pipeline in Python that answered 120 questions a day."),
    )
    assert "500" in bullet_problem(inflated, keywords, docs)
    invented = _bullet("Built things", ("resume", "Built a rocket"))
    assert "not in candidate document" in bullet_problem(invented, keywords, docs)
    from_job = _bullet("Python", ("job_description", "Python"))
    assert bullet_problem(from_job, keywords, docs)


def test_the_tailor_asks_about_missing_keywords_and_keeps_only_sourced_bullets(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "bank.md").write_text(BANK, encoding="utf-8")
    draft = {
        "sections": [
            {
                "heading": "Experience",
                "entries": [
                    {"entry_id": "e1", "bullets": [CLAIMS_K8S]},
                    {"entry_id": "e2", "bullets": [GOOD]},
                    {"entry_id": "e3", "bullets": [GOOD]},  # dropped from the pool
                ],
            }
        ],
        "skills": [{"label": "Tools", "items": ["Python", "PyTorch", "Rust"]}],
    }
    client = FakeClient([draft, draft])  # the fix round repeats the same mistake
    asked: list[str] = []

    resume = tailor_resume(
        JOB, RESUME, client=client, model="m", library_dir=library,
        ask=lambda keyword: asked.append(keyword) or None,
    )

    # Python and PyTorch are in the documents; the bank says "evaluation harness", not
    # "evaluation pipelines", so that is asked about too.
    assert asked == ["Kubernetes", "evaluation pipelines"]
    assert resume.gaps == ["Kubernetes", "evaluation pipelines"]
    sections = dict(resume.sections)
    lab, evaluator = sections["Experience"]
    assert lab.bullets == [
        "Built a retrieval pipeline in Python that answered 120 questions a day."
    ]  # the Kubernetes claim failed twice, so the original wording stays
    assert evaluator.bullets == [GOOD["text"]]
    assert dict(resume.skills) == {"Tools": ["Python", "PyTorch"]}  # no source mentions Rust
    assert "evaluate" in resume.used and "Kubernetes" not in resume.used
    fix_round = client.requests[-1]["messages"][-1]["content"]
    assert "Kubernetes" in fix_round
    out = write_docx(resume, tmp_path / "resume.docx")
    from docx import Document

    text = "\n".join(paragraph.text for paragraph in Document(str(out)).paragraphs)
    assert "Sam Sample" in text and GOOD["text"] in text


def test_an_answer_to_a_missing_keyword_becomes_evidence(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    deployed = _bullet(
        "Deployed the evaluator on Kubernetes",
        ("keyword_answers.md", "Kubernetes: deployed the evaluator on a Kubernetes cluster"),
    )
    draft = {
        "sections": [
            {"heading": "Projects", "entries": [{"entry_id": "e2", "bullets": [deployed]}]}
        ],
        "skills": [],
    }
    (library / "bank.md").write_text(BANK, encoding="utf-8")

    resume = tailor_resume(
        JOB, RESUME, client=FakeClient([draft]), model="m", library_dir=library,
        ask=lambda keyword: (
            "deployed the evaluator on a Kubernetes cluster" if keyword == "Kubernetes" else None
        ),
    )

    assert resume.gaps == ["evaluation pipelines"]
    assert dict(resume.sections)["Projects"][0].bullets == [deployed["text"]]
    saved = (library / "keyword_answers.md").read_text(encoding="utf-8")
    assert "Kubernetes: deployed the evaluator" in saved


def test_every_question_is_answered_including_the_common_ones() -> None:
    recorded = {
        "First name": "Sam",
        "Why are you interested in Acme?": None,
        "Agent summary": "Done.",
    }
    questions = questions_for_job("Acme", "AI Engineer", recorded)

    asked = [question for question, _ in questions]
    assert "Agent summary" not in asked
    assert "Why do you want to work at Acme?" not in asked  # the form already asks it
    assert "Why are you a good fit for the AI Engineer role?" in asked
    assert "Tell us about a technical project you are proud of." in asked

    def answerer(question: str, long_form: bool) -> AnswerDecision:
        if long_form:
            return AnswerDecision(f"Draft for {question}", "[resume] x", True, "Review.", True)
        return AnswerDecision(None, None, True, "No source.")

    answers = {item.question: item for item in answer_questions(questions, answerer)}
    assert answers["First name"] == KitAnswer("First name", "Sam", "answer",
                                              "Filled from your profile on the form.")
    assert answers["Why are you interested in Acme?"].kind == "draft"
    assert answers["Tell us about a technical project you are proud of."].kind == "draft"


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def test_the_kit_is_recorded_and_shown_in_the_apply_panel(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apply_kit, "KITS_DIR", tmp_path / "kits")
    monkeypatch.setattr(dashboard, "KITS_DIR", tmp_path / "kits")
    job = Job(
        source="lever", platform="lever", company="Acme", title="AI Engineer",
        url="https://jobs.lever.co/acme/1", location_raw="Remote - US",
        location_category="remote_us", description=DESCRIPTION, status="new",
    )
    session.add(job)
    session.flush()
    session.add(
        Application(job_id=job.id, mode="dry_run", answers={"First name": "Sam", "Why Acme?": None})
    )
    session.flush()
    session.refresh(job)
    library = tmp_path / "library"
    library.mkdir()
    (library / "bank.md").write_text(BANK, encoding="utf-8")
    monkeypatch.setattr("agent.resume_tailor.LIBRARY_DIR", library)
    draft = {
        "sections": [{"heading": "Projects", "entries": [{"entry_id": "e2", "bullets": [GOOD]}]}],
        "skills": [],
    }

    kit = apply_kit.build_kit(
        session, job, profile={}, resume_text=RESUME, client=FakeClient([draft]),
        answer_model="m", resume_model="m", ask=None,
        answerer=lambda question, _long: AnswerDecision(f"Draft: {question}", "e", True, "r", True),
    )

    assert not kit.problems and kit.resume_path.is_file()
    session.refresh(job)
    stored = apply_kit.latest_kit(job)
    assert stored.answers["First name"] == "Sam"
    assert stored.suggested_answers["Why Acme?"] == "Draft: Why Acme?"
    assert "Kubernetes" in stored.field_notes["Resume keywords"]
    page = tmp_path / "dashboard.html"
    dashboard.write_dashboard(session, page, fit_threshold=70)
    html = page.read_text(encoding="utf-8")
    assert f"href='/kits/{job.id}/resume'" in html
    assert "Draft: Tell us about a technical project you are proud of." in html
    assert "class='apply-link' target='_blank'" in html
