"""The form agent on a real browser page with custom widgets, driven by a scripted model."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from agent.answers import AnswerDecision
from agent.form_agent import FormAgent, run_form_agent
from agent.types import JobListing

FORM = """<!doctype html><html><body>
<h1>Apply: AI Engineer</h1>
<label for="name">Legal name</label><input id="name">
<label for="email">Email address</label><input id="email" type="email">
<div><span id="country-label">Country</span>
  <div role="combobox" aria-labelledby="country-label" tabindex="0" id="country"
       onclick="document.getElementById('countries').hidden = false">Select...</div>
  <ul role="listbox" id="countries" hidden>
    <li role="option" onclick="pick('United States')">United States</li>
    <li role="option" onclick="pick('Canada')">Canada</li>
  </ul></div>
<fieldset><legend>Will you require sponsorship?</legend>
  <label><input type="radio" name="visa" value="yes"> Yes</label>
  <label><input type="radio" name="visa" value="no"> No</label></fieldset>
<label for="why">Why do you want to work here? What draws you to this team?</label>
<textarea id="why"></textarea>
<label for="cv">Resume/CV</label><input id="cv" type="file">
<label for="pw">Password</label><input id="pw" type="password">
<button type="button" onclick="window.accountCreated = true">Create Account</button>
<button type="button" onclick="window.submitted = true">Submit Application</button>
<script>function pick(value) {
  document.getElementById('country').textContent = value;
  document.getElementById('countries').hidden = true;
}</script>
</body></html>"""


def call(name: str, arguments: dict, number: int) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=arguments, id=f"call{number}")


class ScriptedModel:
    """Plays the model: each step reads the latest observation and returns tool calls."""

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.results: list[dict] = []

    def create(self, **request):
        history = request["messages"]
        if history[-1]["role"] == "user" and isinstance(history[-1]["content"], list):
            self.results.extend(history[-1]["content"])
        observation = next(
            (
                json.loads(result["content"])
                for result in reversed(self.results)
                if isinstance(result["content"], str) and '"controls"' in result["content"]
            ),
            {"controls": []},
        )
        step = self.steps.pop(0)
        calls = step(_ids(observation))
        return SimpleNamespace(stop_reason="tool_use", content=calls)


def _ids(observation: dict) -> dict[str, str]:
    """Control ids by a readable key: label, text, or label of the option's button."""
    ids: dict[str, str] = {}
    for control in observation["controls"]:
        for key in (control.get("label"), control.get("text")):
            if key:
                ids.setdefault(key.split(" | ")[0], control["id"])
    return ids


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(chromium_sandbox=True)
        page = browser.new_page()
        page.set_content(FORM)
        yield page
        browser.close()


def test_agent_fills_custom_widgets_and_never_submits(page, tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    draft = "I build agents that people trust."
    model = ScriptedModel(
        [
            lambda ids: [call("observe_page", {}, 1)],
            lambda ids: [
                call(
                    "fill_field",
                    {
                        "control_id": ids["Legal name"],
                        "value": "Sam Sample",
                        "question": "Legal name",
                    },
                    2,
                ),
                call(
                    "fill_field",
                    {
                        "control_id": ids["Email address"],
                        "value": "sam@example.com",
                        "question": "Email",
                    },
                    3,
                ),
                call(
                    "click",
                    {"control_id": ids["No"], "question": "Will you require sponsorship?"},
                    4,
                ),
                call("upload_resume", {"control_id": ids["Resume/CV"]}, 5),
                call("click", {"control_id": ids["Country"], "question": None}, 6),
            ],
            lambda ids: [call("observe_page", {}, 7)],
            lambda ids: [
                call("click", {"control_id": ids["United States"], "question": "Country"}, 8),
                call(
                    "answer_question",
                    {"question": "Why do you want to work here?", "long_form": True},
                    9,
                ),
                call(
                    "fill_field",
                    {"control_id": ids["Password"], "value": "hunter2", "question": "Password"},
                    10,
                ),
                call("click", {"control_id": ids["Create Account"], "question": None}, 11),
                call("click", {"control_id": ids["Submit Application"], "question": None}, 12),
            ],
            lambda ids: [
                call(
                    "finish",
                    {
                        "summary": "Filled everything but the essay.",
                        "left_for_candidate": [
                            {"field": "Password", "reason": "Sign-in is yours."},
                            {"field": "Submit Application", "reason": "Yours to submit."},
                        ],
                    },
                    13,
                )
            ],
        ]
    )
    answers = {
        "Why do you want to work here?": AnswerDecision(
            draft, "[resume] agents", True, "Draft for review.", is_motivation_draft=True
        )
    }

    agent = FormAgent(
        page,
        profile={"personal": {"name": "Sam Sample"}},
        resume_path=resume,
        answerer=lambda question, _long: answers[question],
        client=model,
        model="test-model",
    )
    run = agent.start(JobListing("x", "x", "Acme", "AI Engineer", "https://acme.example", "", ""))

    assert run.finished and run.summary == "Filled everything but the essay."
    assert page.input_value("#name") == "Sam Sample"
    assert page.input_value("#email") == "sam@example.com"
    assert page.is_checked("input[value=no]")
    assert page.text_content("#country") == "United States"
    assert page.evaluate("document.getElementById('cv').files[0].name") == "resume.pdf"
    assert page.input_value("#pw") == ""
    assert page.evaluate("window.submitted") is None
    assert page.evaluate("window.accountCreated") is None
    assert page.input_value("#why") == ""
    assert run.answers["Country"] == "United States"
    assert run.answers["Will you require sponsorship?"] == "No"
    assert run.suggested_answers["Why do you want to work here?"] == draft
    assert run.answers["Password"] is None
    assert "Submit Application" not in run.answers
    refusals = [r["content"] for r in model.results if r.get("is_error")]
    assert any("would submit" in text for text in refusals)
    assert any("account creation" in text for text in refusals)
    assert any("passwords" in text for text in refusals)
    essay = next(r["content"] for r in model.results if "draft" in r["content"])
    assert "do not fill" in essay


def test_drafts_are_typed_in_only_when_allowed(page, tmp_path: Path) -> None:
    decision = AnswerDecision("A draft.", None, True, "Review.", is_motivation_draft=True)
    allowed = FormAgent(
        page,
        profile={},
        resume_path=tmp_path / "r.pdf",
        answerer=lambda *_: decision,
        client=None,
        model="m",
        fill_drafts=True,
    )
    assert allowed._tool_answer_question("Why us?", True)["action"].startswith("fill it")
    assert allowed.run.suggested_answers == {"Why us?": "A draft."}


def test_without_a_person_logins_are_reported_not_waited_for(page, tmp_path: Path) -> None:
    agent = FormAgent(
        page,
        profile={},
        resume_path=tmp_path / "r.pdf",
        answerer=lambda *_: None,
        client=None,
        model="m",
    )

    result = agent._tool_ask_human("Sign in to Workday")

    assert "Nobody is at the browser" in result["error"]
    assert agent.run.notes["Needed the candidate"] == "Sign in to Workday"


def test_run_form_agent_reports_like_the_other_fillers(
    page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("personal:\n  name: Sam Sample\n  email: sam@example.com\n", "utf-8")
    monkeypatch.setattr("agent.form_agent.load_profile", lambda _path: {"personal": {}})
    monkeypatch.setattr("agent.form_agent.extract_resume_text", lambda _path: "Python.")
    monkeypatch.setattr(page, "goto", lambda *_args, **_kwargs: None)
    model = ScriptedModel(
        [
            lambda ids: [call("observe_page", {}, 1)],
            lambda ids: [
                call(
                    "fill_field",
                    {"control_id": ids["Legal name"], "value": "Sam Sample", "question": "Name"},
                    2,
                )
            ],
            lambda ids: [call("finish", {"summary": "Done.", "left_for_candidate": []}, 3)],
        ]
    )

    result = run_form_agent(
        page,
        JobListing("x", "x", "Acme", "AI Engineer", "https://acme.example", "", ""),
        profile,
        tmp_path / "resume.pdf",
        client=model,
        model="test-model",
        screenshot_path=tmp_path / "shot.png",
    )

    assert result.answers == {"Name": "Sam Sample"}
    assert result.field_notes["Agent summary"] == "Done."
    assert "attach it yourself" in result.field_notes["Resume"]
    assert result.status == "dry_run_ready"
    assert Path(result.screenshot_path).is_file()


ZOHO_LIKE = """<!doctype html><html><body>
<div id="cookie" style="position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.3)">
  <button type="button" onclick="document.getElementById('cookie').remove()">Accept all</button>
</div>
<div class="field"><label>Resume</label>
  <span class="browse">Browse<input type="file" style="display:none" id="resume"></span></div>
<div class="field"><label>Tools you use</label>
  <label><input type="checkbox" id="openai" style="opacity:0;position:absolute">
    <span>OpenAI API</span></label></div>
</body></html>"""


def test_hidden_uploads_cookie_overlays_and_styled_checkboxes(page, tmp_path: Path) -> None:
    page.set_content(ZOHO_LIKE)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    agent = FormAgent(
        page, profile={}, resume_path=resume, answerer=lambda *_: None, client=None, model="m"
    )
    controls = agent._tool_observe_page()["controls"]
    ids = _ids({"controls": controls})
    upload = next(item for item in controls if item.get("type") == "file")
    assert upload["label"] == "Resume"

    blocked = agent._tool_click(ids["OpenAI API"], "Tools you use")
    assert "covers this control" in blocked["error"] and "cookie" in blocked["error"]

    agent._tool_click(ids["Accept all"])
    controls = agent._tool_observe_page()["controls"]
    ids = _ids({"controls": controls})
    upload = next(item for item in controls if item.get("type") == "file")
    assert agent._tool_click(ids["OpenAI API"], "Tools you use")["checked"] is True
    assert agent.run.answers["Tools you use"] == "OpenAI API"
    agent._tool_upload_resume(upload["id"])
    assert page.evaluate("document.getElementById('resume').files[0].name") == "resume.pdf"


def test_finish_notes_attach_to_the_question_already_recorded(page, tmp_path: Path) -> None:
    agent = FormAgent(
        page, profile={}, resume_path=tmp_path / "r.pdf", answerer=None, client=None, model="m"
    )
    agent.run.answers["Expected Salary"] = None
    agent.run.answers["Experience in Years"] = None

    agent._tool_finish(
        "Done.",
        [
            {"field": "Expected Salary (required)", "reason": "Yours to decide."},
            {"field": "Typing Speed (WPM)", "reason": "Not in your profile."},
        ],
    )

    assert set(agent.run.answers) == {
        "Expected Salary",
        "Experience in Years",
        "Typing Speed (WPM)",
    }
    assert agent.run.notes["Expected Salary"] == "Yours to decide."


TWO_PAGES = """<!doctype html><html><body>
<section id="one"><h2>Your information</h2>
  <label for="name">Full name</label><input id="name">
  <label for="salary">Expected salary (required)</label><input id="salary" required>
  <p id="error" role="alert" hidden>Please fill in all required fields.</p>
  <button type="button" onclick="next()">Next</button></section>
<section id="two" hidden><h2>Questions</h2>
  <label for="why">Why do you want to work at Acme?</label><textarea id="why"></textarea>
  <button type="button" onclick="window.submitted = true">Submit application</button></section>
<script>function next() {
  if (!document.getElementById('salary').value) {
    document.getElementById('error').hidden = false; return;
  }
  document.getElementById('one').hidden = true;
  document.getElementById('two').hidden = false;
}</script></body></html>"""


def test_the_candidate_fills_a_blocking_field_and_the_agent_answers_the_next_page(
    page, tmp_path: Path
) -> None:
    page.set_content(TWO_PAGES)
    asked: list[str] = []

    def person_fills(request: str) -> bool:
        asked.append(request)
        page.fill("#salary", "Negotiable")
        return True

    draft = "Acme's agent platform matches the evaluation work I have done."
    model = ScriptedModel(
        [
            lambda ids: [call("observe_page", {}, 1)],
            lambda ids: [
                call(
                    "fill_field",
                    {"control_id": ids["Full name"], "value": "Sam Sample", "question": "Name"},
                    2,
                ),
                call("click", {"control_id": ids["Next"], "question": None}, 3),
                call("observe_page", {}, 4),
            ],
            lambda ids: [call("ask_human", {"request": "Fill in Expected salary."}, 5)],
            lambda ids: [call("observe_page", {}, 6)],
            lambda ids: [call("click", {"control_id": ids["Next"], "question": None}, 7)],
            lambda ids: [call("observe_page", {}, 8)],
            lambda ids: [
                call(
                    "answer_question",
                    {"question": "Why do you want to work at Acme?", "long_form": True},
                    9,
                )
            ],
            lambda ids: [
                call(
                    "fill_field",
                    {
                        "control_id": ids["Why do you want to work at Acme?"],
                        "value": draft,
                        "question": "Why do you want to work at Acme?",
                    },
                    10,
                ),
                call("click", {"control_id": ids["Submit application"], "question": None}, 11),
            ],
            lambda ids: [
                call("finish", {"summary": "Both pages done.", "left_for_candidate": []}, 12)
            ],
        ]
    )
    decision = AnswerDecision(
        draft, "[resume] evaluation", True, "Draft.", is_motivation_draft=True
    )
    agent = FormAgent(
        page,
        profile={},
        resume_path=tmp_path / "r.pdf",
        answerer=lambda *_: decision,
        client=model,
        model="m",
        ask_human=person_fills,
        fill_drafts=True,
    )

    run = agent.start(JobListing("x", "x", "Acme", "AI Engineer", "https://acme.example", "", ""))

    assert asked == ["Fill in Expected salary."]
    assert page.is_visible("#why") and page.input_value("#why") == draft
    assert page.evaluate("window.submitted") is None
    assert run.answers["Name"] == "Sam Sample"
    assert run.suggested_answers["Why do you want to work at Acme?"] == draft
    assert run.finished


ZOHO_FIELDS = """<!doctype html><html><head><style>
.box { position: relative; display: inline-block; width: 16px; height: 16px; }
.box input { position: absolute; inset: 0; margin: 0; opacity: 0; }
.box span { position: absolute; inset: 0; border: 1px solid #333; }
</style></head><body>
<div class="row"><div class="fieldLabel">Experience in Years *</div>
  <div class="value"><input type="text" id="years"></div></div>
<div class="row"><div class="fieldLabel">Applicant Type *</div>
  <div class="value"><div role="combobox" tabindex="0" id="type">-None-</div></div></div>
<div class="row"><div class="fieldLabel">Which AI tools have you used? *</div>
  <div class="value">
    <div><span class="box"><input type="checkbox" name="tools" id="openai"><span></span></span>
      <label>OpenAI API</label></div>
    <div><span class="box"><input type="checkbox" name="tools" id="faiss"><span></span></span>
      <label>FAISS</label></div></div></div>
</body></html>"""


def test_unlinked_questions_are_read_and_styled_checkboxes_get_checked(
    page, tmp_path: Path
) -> None:
    page.set_content(ZOHO_FIELDS)
    agent = FormAgent(
        page, profile={}, resume_path=tmp_path / "r.pdf", answerer=None, client=None, model="m"
    )

    controls = agent._tool_observe_page()["controls"]

    by_tag = {(item.get("type") or item.get("role") or item["tag"]): item for item in controls}
    assert by_tag["text"]["label"] == "Experience in Years *"
    assert by_tag["combobox"]["label"] == "Applicant Type *"
    boxes = [item for item in controls if item.get("type") == "checkbox"]
    assert {item["group"] for item in boxes} == {"Which AI tools have you used? *"}
    result = agent._tool_click(boxes[0]["id"], "Which AI tools have you used?")
    assert result["checked"] is True and page.is_checked("#openai")
    assert agent.run.answers["Which AI tools have you used?"]
