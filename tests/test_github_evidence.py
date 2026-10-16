"""The agent finds a skill in the candidate's GitHub repositories and treats "X or Y" as one."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent import apply_kit, resume_tailor
from agent.github_evidence import dependency_names, github_user, load_github_documents
from agent.library import LibraryDocument
from agent.resume_tailor import (
    KEYWORD_PROMPT,
    POOL_PROMPT,
    SUGGEST_PROMPT,
    Keywords,
    _verified_alternatives,
    find_missing,
    keyword_report,
    qualification_map,
    relevant_repositories,
    tailor_resume,
)

# These tests' fictional entries have one fact each; see the fixture.
pytestmark = pytest.mark.usefixtures("one_bullet_entries")

RESUME = """Jordan Example
jordan@example.com
Research Assistant, Example Lab
Evaluated LLM agents in Python on 40 tasks."""
DESCRIPTION = (
    "<h3>Requirements</h3><ul><li>Experience with Python.</li>"
    "<li>Know LangGraph or LangChain.</li><li>Experience with Kubernetes.</li></ul>"
)
JOB = {"company": "Acme", "title": "AI Engineer", "description": DESCRIPTION}
README = "# ticket-agent\nA support agent that answers tickets from a help center."
NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _repos_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        url = str(request.url)
        if url.startswith("https://api.github.com/users/jordan-example/repos"):
            return httpx.Response(200, json=[
                {"name": "ticket-agent", "html_url": "https://github.com/jordan-example/ticket-agent",
                 "description": "Support ticket agent", "language": "Python",
                 "topics": ["llm"], "fork": False},
                {"name": "someone-elses", "fork": True},
            ])
        if url.endswith("/repos/jordan-example/ticket-agent/readme"):
            return httpx.Response(200, text=README)
        if url.endswith("/ticket-agent/HEAD/requirements.txt"):
            return httpx.Response(200, text="# deps\nlangchain_openai>=0.2\nfastapi==0.110\n-e .\n")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_dependency_files_name_their_libraries() -> None:
    assert dependency_names("requirements.txt", "LangChain_Core>=0.3\n# c\nnumpy\n-r a.txt") == [
        "langchain-core", "numpy"]
    pyproject = (
        '[project]\ndependencies = ["langgraph>=0.2", "httpx"]\n'
        '[project.optional-dependencies]\ndev = ["pytest"]\n'
        '[tool.poetry.dependencies]\npython = "^3.11"\nfaiss_cpu = "*"\n'
    )
    assert dependency_names("pyproject.toml", pyproject) == [
        "langgraph", "httpx", "pytest", "faiss-cpu"]
    package = json.dumps({"dependencies": {"react": "1"}, "devDependencies": {"vite": "5"}})
    assert dependency_names("package.json", package) == ["react", "vite"]


def test_the_github_user_comes_from_the_environment_or_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("personal:\n  github: https://github.com/jordan-example/\n", "utf-8")
    monkeypatch.delenv("GITHUB_USERNAME", raising=False)
    assert github_user(profile) == "jordan-example"
    monkeypatch.setenv("GITHUB_USERNAME", "other-user")
    assert github_user(profile) == "other-user"
    monkeypatch.setenv("GITHUB_USERNAME", "not a user/../x")
    assert github_user(profile) is None
    monkeypatch.delenv("GITHUB_USERNAME")
    assert github_user(tmp_path / "missing.yaml") is None


def test_repositories_become_documents_and_are_cached_for_a_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls: list[str] = []
    client = httpx.Client(transport=_repos_transport(calls))

    documents = load_github_documents(
        "jordan-example", client=client, cache_dir=tmp_path, now=NOW
    )

    assert [doc.doc_id for doc in documents] == ["github:ticket-agent"]  # forks are skipped
    text = documents[0].text
    assert "Main language: Python" in text
    assert "Libraries used (requirements.txt): langchain-openai, fastapi" in text
    assert README in text
    assert resume_tailor._mentioned("LangChain", documents)  # langchain_openai counts

    calls.clear()
    again = load_github_documents(
        "jordan-example", client=client, cache_dir=tmp_path, now=NOW + timedelta(hours=23)
    )
    assert again == documents and calls == []

    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    stale = load_github_documents(
        "jordan-example",
        client=httpx.Client(transport=httpx.MockTransport(broken)),
        cache_dir=tmp_path,
        now=NOW + timedelta(days=3),
    )
    assert stale == documents  # an old copy beats none
    assert load_github_documents(
        "nobody", client=httpx.Client(transport=httpx.MockTransport(broken)), cache_dir=tmp_path
    ) == []


def test_alternatives_are_kept_only_when_the_posting_offers_a_choice() -> None:
    text = (
        "Know LangGraph or LangChain. Python and SQL required. "
        "A framework such as PyTorch, JAX, or TensorFlow."
    )
    assert _verified_alternatives(
        [["LangGraph", "LangChain"], ["Python", "SQL"], ["PyTorch", "JAX", "TensorFlow"],
         ["LangChain", "CrewAI"], "not a group"],
        text,
    ) == [["LangGraph", "LangChain"], ["PyTorch", "JAX", "TensorFlow"]]


def test_one_alternative_found_meets_the_requirement() -> None:
    keywords = Keywords(
        technologies=["Python", "LangGraph", "LangChain", "Kubernetes"],
        alternatives=[["LangGraph", "LangChain"]],
    )
    repo = LibraryDocument("github:x", "GitHub repository x", "Libraries used: langchain-core")
    resume = LibraryDocument("resume", "Resume", "Python")

    assert find_missing(keywords, [resume, repo]) == (["Kubernetes"], ["LangGraph"])
    # Neither found: the pair is one gap, reported once.
    assert find_missing(keywords, [resume]) == (["LangGraph", "Kubernetes"], ["LangChain"])


def test_only_repositories_naming_the_jobs_keywords_are_searched() -> None:
    keywords = Keywords(technologies=["LangChain", "Python"])
    both = LibraryDocument("github:a", "a", "Python with langchain")
    one = LibraryDocument("github:b", "b", "Python")
    none = LibraryDocument("github:c", "c", "Rust")
    assert relevant_repositories([one, none, both], keywords) == [both, one]
    assert relevant_repositories([one, both], keywords, limit=1) == [both]


REPO_TEXT = (
    "ticket-agent (GitHub repository: https://github.com/jordan-example/ticket-agent)\n"
    "Description: Support ticket agent\nMain language: Python\n"
    "Libraries used (requirements.txt): langchain-openai, fastapi\n"
    "README:\nA support agent that answers tickets from a help center."
)


class FakeClient:
    """Keywords with an "or" pair, a pool with a GitHub project, then one tailored draft."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.messages = self.beta.messages

    def create(self, **request):
        self.requests.append(request)
        if request["system"] == KEYWORD_PROMPT:
            payload = {
                "action_verbs": ["Built"],
                "technologies": ["Python", "LangGraph", "LangChain", "Kubernetes"],
                "concepts": [], "required": ["Python"], "preferred": [],
                "alternatives": [["LangGraph", "LangChain"]],
                "qualifications": [{"kind": "required", "text": "Know LangGraph or LangChain",
                                    "terms": ["LangGraph", "LangChain"]}],
            }
        elif request["system"] == POOL_PROMPT:
            blank = {"organization": "", "location": "", "dates": ""}
            payload = {
                "name": "Jordan Example", "contact": [], "education": [],
                "entries": [{**blank, "title": "ticket-agent", "kind": "project",
                             "doc_id": "github:ticket-agent",
                             "facts": ["Libraries used (requirements.txt): langchain-openai"]}],
            }
        elif request["system"] == SUGGEST_PROMPT:
            payload = {"suggestions": []}
        else:
            payload = {
                "sections": [{"heading": "Projects", "entries": [{"entry_id": "e1", "bullets": [{
                    "text": "Built a support ticket agent in Python with LangChain",
                    "evidence": [
                        {"doc_id": "github:ticket-agent", "quote": "Support ticket agent"},
                        {"doc_id": "github:ticket-agent", "quote": "Main language: Python"},
                        {"doc_id": "github:ticket-agent", "quote": "langchain-openai"},
                    ],
                }]}]}],
                "skills": [{"label": "Tools", "items": ["Python", "LangChain"]}],
            }
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )


def test_the_agent_uses_github_evidence_and_asks_nothing(tmp_path: Path) -> None:
    client = FakeClient()
    repo = LibraryDocument("github:ticket-agent", "GitHub repository ticket-agent", REPO_TEXT)
    unrelated = LibraryDocument("github:dotfiles", "GitHub repository dotfiles", "Shell config")

    resume = tailor_resume(
        JOB, RESUME, client=client, model="m", library_dir=tmp_path / "library",
        github_documents=[unrelated, repo], ask=None,
    )

    pool_request = next(r for r in client.requests if r["system"] == POOL_PROMPT)
    sent = [doc["doc_id"] for doc in json.loads(pool_request["messages"][0]["content"])]
    assert "github:ticket-agent" in sent and "github:dotfiles" not in sent
    bullet = "Built a support ticket agent in Python with LangChain"
    assert resume.sections[0][1][0].bullets == [bullet]
    assert resume.gaps == ["Kubernetes"]  # LangGraph is met by LangChain
    assert "LangGraph" not in resume.unused
    row = qualification_map(resume)[0]
    assert row["matches"][0]["terms"] == ["LangChain"] and row["missing"] == []
    assert "No evidence in your documents (1): Kubernetes" in keyword_report(resume)

    # Even with someone to ask, the pair is not asked about once one of it is found.
    asked: list[str] = []
    tailor_resume(
        JOB, RESUME, client=FakeClient(), model="m", library_dir=tmp_path / "library",
        github_documents=[repo], ask=lambda keyword, _s: asked.append(keyword) or None,
    )
    assert asked == ["Kubernetes"]


def test_job_kit_asks_only_when_told_to() -> None:
    parser = apply_kit.build_parser()
    assert parser.parse_args(["--job-id", "1"]).ask is False
    assert parser.parse_args(["--job-id", "1", "--ask"]).ask is True
    assert parser.parse_args(["--job-id", "1", "--no-questions"]).no_questions is True
