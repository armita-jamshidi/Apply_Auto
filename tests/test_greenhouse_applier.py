"""Mocked tests for the Greenhouse dry-run browser workflow."""

import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pydantic import ValidationError

from agent.answers import AnswerDecision
from agent.applier.greenhouse import load_profile, run_greenhouse_dry_run
from agent.types import JobListing


class FakeField:
    def __init__(
        self,
        label: str,
        tag: str = "input",
        field_type: str = "text",
        role: str | None = None,
        options: list[str] | None = None,
    ) -> None:
        self.label = label
        self.tag = tag
        self.field_type = field_type
        self.role = role
        self.options = options or []
        self.value: str | None = None
        self.uploaded_file: str | None = None

    @property
    def first(self) -> "FakeField":
        return self

    def count(self) -> int:
        return 1

    def fill(self, value: str) -> None:
        self.value = value

    def get_attribute(self, name: str) -> str | None:
        if name == "type":
            return self.field_type
        if name == "role":
            return self.role
        return None

    def evaluate(self, _expression: str) -> str:
        if "input.files" in _expression:
            return bool(self.uploaded_file)
        if "checkValidity" in _expression:
            return True
        return self.tag

    def set_input_files(self, path: str) -> None:
        self.uploaded_file = path

    def locator(self, selector: str) -> SimpleNamespace:
        assert selector == "option"
        return SimpleNamespace(all_text_contents=lambda: self.options)

    def select_option(self, *, label: str) -> None:
        self.value = label

    def click(self) -> None:
        return None

    def input_value(self) -> str:
        return self.value or ""


class FakeLabel:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self) -> str:
        return self.text


class FakeLabels:
    def __init__(self, labels: list[str]) -> None:
        self.labels = [FakeLabel(label) for label in labels]

    def count(self) -> int:
        return len(self.labels)

    def nth(self, index: int) -> FakeLabel:
        return self.labels[index]


class FakePage:
    def __init__(self, fields: list[FakeField]) -> None:
        self.fields = {field.label: field for field in fields}
        self.navigated_to: str | None = None
        self.screenshots: list[tuple[str, bool]] = []

    def goto(self, url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"
        self.navigated_to = url

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        assert state == "networkidle"
        assert timeout > 0

    def locator(self, selector: str) -> FakeLabels | FakeField | SimpleNamespace:
        if selector == "#application-form label":
            return FakeLabels(list(self.fields))
        if selector == "#application-form":
            return SimpleNamespace(evaluate=lambda _expression: True)
        if selector == "#resume":
            resume_field = next(
                (field for field in self.fields.values() if field.field_type == "file"),
                None,
            )
            return resume_field or SimpleNamespace(count=lambda: 0)
        raise AssertionError(f"Unexpected locator selector: {selector}")

    def get_by_label(self, label: str, *, exact: bool) -> FakeField | SimpleNamespace:
        if exact:
            field = self.fields.get(label)
        else:
            field = next(
                (item for name, item in self.fields.items() if label.casefold() in name.casefold()),
                None,
            )
        return field if field is not None else SimpleNamespace(count=lambda: 0)

    def get_by_role(self, role: str, *, name: str, exact: bool) -> FakeField | SimpleNamespace:
        if role == "option":
            matches = [
                field
                for field in self.fields.values()
                if field.role == "combobox" and name in field.options
            ]
            if not matches:
                return SimpleNamespace(count=lambda: 0)
            field = matches[0]
            return SimpleNamespace(count=lambda: 1, click=lambda: setattr(field, "value", name))
        matches = [
            field
            for field in self.fields.values()
            if field.role == role
            and (field.label == name if exact else name.casefold() in field.label.casefold())
        ]
        return matches[0] if matches else SimpleNamespace(count=lambda: 0)

    def get_by_text(self, text: str, *, exact: bool) -> SimpleNamespace:
        assert exact
        attached = any(
            field.uploaded_file and Path(field.uploaded_file).name == text
            for field in self.fields.values()
        )

        def wait_for(*, state: str, timeout: int) -> None:
            assert state == "visible"
            assert timeout > 0
            if not attached:
                raise PlaywrightTimeoutError("Attachment filename did not appear")

        return SimpleNamespace(wait_for=wait_for)

    def screenshot(self, *, path: str, full_page: bool) -> None:
        self.screenshots.append((path, full_page))
        Path(path).write_bytes(b"fake screenshot")


def mock_answer_client(answer: str | None, evidence: str | None) -> Mock:
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="submit_grounded_answer",
                input={"answer": answer, "evidence": evidence},
            )
        ]
    )
    return client


def make_job() -> JobListing:
    return JobListing(
        source="greenhouse",
        platform="greenhouse",
        company="Example Co",
        title="Software Engineer",
        url="https://boards.greenhouse.io/example/jobs/123",
        location_raw="Remote - US",
        description="Build software.",
    )


def create_profile_and_resume(tmp_path: Path) -> tuple[Path, Path]:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "personal:\n"
        "  name: Alex Candidate\n"
        "  email: alex@example.com\n"
        "  phone: '+1-555-0100'\n"
        "  linkedin: https://example.com/alex\n"
        "  website: https://alex.example.com\n"
        "  github: https://github.com/alex\n"
        "skills:\n"
        "  - Python\n",
        encoding="utf-8",
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"fake pdf for mocked browser test")
    return profile_path, resume_path


def test_dry_run_fills_standard_fields_uploads_and_screenshots(tmp_path: Path) -> None:
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Email", field_type="email"),
            FakeField("Phone", field_type="tel"),
            FakeField("LinkedIn", field_type="url"),
            FakeField("Website", field_type="url"),
            FakeField("GitHub URL", field_type="url"),
            FakeField("Resume/CV", field_type="file"),
            FakeField("What programming language do you use?", tag="textarea"),
        ]
    )
    screenshot_path = tmp_path / "screenshots" / "filled.png"

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        screenshot_path,
        answers_client=mock_answer_client("Python", "Python"),
        resume_text="Python developer.",
    )

    assert result.status == "dry_run_ready"
    assert page.navigated_to == make_job().url
    assert page.fields["First Name"].value == "Alex"
    assert page.fields["Last Name"].value == "Candidate"
    assert page.fields["Email"].value == "alex@example.com"
    assert page.fields["LinkedIn"].value == "https://example.com/alex"
    assert page.fields["GitHub URL"].value == "https://github.com/alex"
    assert page.fields["Resume/CV"].uploaded_file == str(resume_path)
    assert result.resume_uploaded is True
    assert page.fields["What programming language do you use?"].value == "Python"
    assert result.answers["What programming language do you use?"] == "Python"
    assert result.screenshot_path == str(screenshot_path)
    assert page.screenshots == [(str(screenshot_path), True)]
    assert screenshot_path.is_file()
    assert not hasattr(page, "submit")


def test_ungrounded_custom_answer_stays_blank_and_routes_to_manual_review(
    tmp_path: Path,
) -> None:
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Email", field_type="email"),
            FakeField("Resume/CV", field_type="file"),
            FakeField("How many years of experience do you have?", tag="textarea"),
        ]
    )

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        tmp_path / "filled.png",
        answers_client=mock_answer_client("Eight years", "Eight years"),
        resume_text="Python developer.",
    )

    assert result.status == "manual_review"
    assert page.fields["How many years of experience do you have?"].value is None
    assert result.answers["How many years of experience do you have?"] is None
    assert result.screenshot_path is not None


def test_combobox_is_left_blank_and_routed_to_manual_review(tmp_path: Path) -> None:
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Resume/CV", field_type="file"),
            FakeField("Country", tag="input", role="combobox", options=["Canada", "Other"]),
        ]
    )
    answer_client = mock_answer_client("United States", "United States")

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        tmp_path / "filled.png",
        answers_client=answer_client,
        resume_text="Python developer.",
    )

    assert result.status == "manual_review"
    assert page.fields["Country"].value is None
    assert result.answers["Country"] is None
    answer_client.messages.create.assert_not_called()


@pytest.mark.parametrize(
    ("question", "options", "expected"),
    [
        ("Have you interviewed at Anthropic before?", ["Select...", "Yes", "No"], "No"),
        ("AI Policy for Application", ["Select...", "Yes", "No"], "Yes"),
    ],
)
def test_explicit_policy_selects_supported_choice(
    tmp_path: Path,
    question: str,
    options: list[str],
    expected: str,
) -> None:
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Resume/CV", field_type="file"),
            FakeField(question, role="combobox", options=options),
        ]
    )

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        tmp_path / "filled.png",
        resume_text="Python developer.",
    )

    assert page.fields[question].value == expected
    assert result.answers[question] == expected


def test_qualification_choice_stays_blank_without_explicit_profile_fact(tmp_path: Path) -> None:
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    question = "Do you meet the qualifications for this role?"
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Resume/CV", field_type="file"),
            FakeField(question, role="combobox", options=["Select...", "Yes", "No"]),
        ]
    )

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        tmp_path / "filled.png",
        resume_text="Python developer.",
    )

    assert page.fields[question].value is None
    assert result.answers[question] is None
    assert result.status == "manual_review"


def test_website_is_left_blank_when_it_duplicates_github(tmp_path: Path) -> None:
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    profile_text = profile_path.read_text(encoding="utf-8").replace(
        "https://alex.example.com", "https://github.com/alex"
    )
    profile_path.write_text(profile_text, encoding="utf-8")
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Website", field_type="url"),
            FakeField("GitHub URL", field_type="url"),
            FakeField("Resume/CV", field_type="file"),
        ]
    )

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        tmp_path / "filled.png",
        resume_text="Python developer.",
    )

    assert page.fields["Website"].value is None
    assert page.fields["GitHub URL"].value == "https://github.com/alex"
    assert result.answers["Website"] is None
    assert result.status == "manual_review"


def test_resume_upload_is_not_reported_when_browser_has_no_file(tmp_path: Path) -> None:
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Resume/CV", field_type="file"),
        ]
    )
    page.fields["Resume/CV"].set_input_files = lambda _path: None

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        tmp_path / "filled.png",
        resume_text="Python developer.",
    )

    assert result.resume_uploaded is False
    assert result.status == "manual_review"


@pytest.mark.parametrize("fill_draft", [False, True])
def test_motivation_answer_requires_explicit_opt_in_to_fill(
    tmp_path: Path,
    fill_draft: bool,
) -> None:
    from unittest.mock import patch

    profile_path, resume_path = create_profile_and_resume(tmp_path)
    question = "Why do you want to participate in this program?"
    draft = "I built a Python service for literature analysis."
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Resume/CV", field_type="file"),
            FakeField(question, tag="textarea"),
        ]
    )

    with patch(
        "agent.applier.greenhouse.answer_custom_question",
        return_value=AnswerDecision(
            answer=draft,
            evidence="[profile] Built a Python service for literature analysis.",
            needs_manual_review=True,
            reason="Candidate review is required.",
            is_motivation_draft=True,
        ),
    ):
        result = run_greenhouse_dry_run(
            page,
            make_job(),
            profile_path,
            resume_path,
            tmp_path / "filled.png",
            resume_text="Python developer.",
            fill_reviewed_motivation_drafts=fill_draft,
        )

    assert result.status == ("dry_run_ready" if fill_draft else "manual_review")
    if fill_draft:
        assert page.fields[question].value == draft
        assert result.answers[question] == draft
        assert question not in result.suggested_answers
    else:
        assert page.fields[question].value is None
        assert result.answers[question] is None
        assert result.suggested_answers[question] == draft


def test_missing_resume_upload_control_routes_to_manual_review(tmp_path: Path) -> None:
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    page = FakePage([FakeField("First Name"), FakeField("Last Name")])

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        tmp_path / "filled.png",
        resume_text="Python developer.",
    )

    assert result.status == "manual_review"
    assert result.screenshot_path is not None


def test_invalid_profile_fails_without_opening_job_page(tmp_path: Path) -> None:
    profile_path = tmp_path / "invalid.yaml"
    profile_path.write_text("skills: [Python]\n", encoding="utf-8")
    page = FakePage([])

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        tmp_path / "resume.pdf",
        tmp_path / "filled.png",
        resume_text="resume text",
    )

    assert result.status == "failed"
    assert page.navigated_to is None


def test_profile_loader_preserves_github_and_project_details(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "personal:\n"
        "  name: Alex Candidate\n"
        "  github: https://github.com/alex\n"
        "skills:\n"
        "  - Python\n"
        "projects:\n"
        "  - name: Example Project\n"
        "    date: 2026-01\n"
        "    url: https://example.com/project\n"
        "    summary: Built an example project.\n"
        "    skills: [Python]\n",
        encoding="utf-8",
    )

    loaded = load_profile(profile_path)

    assert loaded["personal"]["github"] == "https://github.com/alex"
    assert loaded["projects"][0]["name"] == "Example Project"
    assert loaded["projects"][0]["skills"] == ["Python"]
    assert loaded["skills"] == ["Python"]


def test_profile_loader_rejects_project_without_name(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "personal:\n  name: Alex Candidate\nprojects:\n  - summary: Missing a project name\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_profile(profile_path)


def test_cli_resume_defaults_to_generic_path_without_resume_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.applier import cli

    monkeypatch.setattr(cli, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("RESUME_PATH", raising=False)

    args = cli.build_parser().parse_args(
        ["--job-url", "https://boards.greenhouse.io/example/jobs/1"]
    )

    assert args.resume == cli.PROJECT_ROOT / "profile" / "resume.pdf"


def test_cli_resume_path_from_env_is_relative_to_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.applier import cli

    monkeypatch.setattr(cli, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("RESUME_PATH", "profile/sample_resume.pdf")

    assert cli.default_resume_path() == cli.PROJECT_ROOT / "profile" / "sample_resume.pdf"


def test_cli_absolute_resume_path_from_env_is_used_as_is(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.applier import cli

    resume = tmp_path / "elsewhere" / "resume.pdf"
    monkeypatch.setattr(cli, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("RESUME_PATH", str(resume))

    assert cli.default_resume_path() == resume


def test_cli_resume_flag_overrides_resume_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.applier import cli

    monkeypatch.setattr(cli, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("RESUME_PATH", "profile/sample_resume.pdf")

    args = cli.build_parser().parse_args(
        ["--job-url", "https://boards.greenhouse.io/example/jobs/1", "--resume", "other.pdf"]
    )

    assert args.resume == Path("other.pdf")


class LiveFakePage(FakePage):
    """Greenhouse page with a submit button and post-submit confirmation text."""

    def __init__(self, fields: list[FakeField]) -> None:
        super().__init__(fields)
        self.submitted = False
        self.visible_text: list[str] = []
        self.confirmation_text: str | None = "Application submitted"
        self.click_error: Exception | None = None

    def get_by_role(self, role: str, *, name: object, exact: bool = False):
        if role == "button":
            return SimpleNamespace(count=lambda: 1, click=self._submit)
        return super().get_by_role(role, name=name, exact=exact)

    def get_by_text(self, text: object, *, exact: bool = False) -> SimpleNamespace:
        if isinstance(text, re.Pattern):
            def matches() -> list[str]:
                return [item for item in self.visible_text if text.search(item)]

            def wait_for(**_kwargs) -> None:
                if not matches():
                    raise PlaywrightTimeoutError("Confirmation is not visible")

            return SimpleNamespace(
                count=lambda: len(matches()), first=SimpleNamespace(wait_for=wait_for)
            )
        return super().get_by_text(text, exact=exact)

    def _submit(self) -> None:
        self.submitted = True
        if self.click_error is not None:
            raise self.click_error
        if self.confirmation_text:
            self.visible_text.append(self.confirmation_text)


def run_live(tmp_path: Path, page: LiveFakePage):
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    return run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        tmp_path / "filled.png",
        resume_text="Python developer.",
        submit_live=True,
    )


def complete_live_page() -> LiveFakePage:
    return LiveFakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Email", field_type="email"),
            FakeField("Resume/CV", field_type="file"),
        ]
    )


def test_greenhouse_live_submit_is_applied_after_confirmation(tmp_path: Path) -> None:
    page = complete_live_page()

    result = run_live(tmp_path, page)

    assert page.submitted is True
    assert result.status == "applied"
    assert result.submitted is True


def test_greenhouse_live_submit_without_confirmation_is_unknown(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = complete_live_page()
    page.confirmation_text = None

    with caplog.at_level(logging.WARNING):
        result = run_live(tmp_path, page)

    assert page.submitted is True
    assert result.status == "unknown"
    assert result.submitted is False
    assert "verify manually" in (result.error or "")
    assert "verify manually" in caplog.text


def test_greenhouse_live_submit_click_error_is_unknown(tmp_path: Path) -> None:
    page = complete_live_page()
    page.click_error = PlaywrightTimeoutError("Navigation interrupted")

    result = run_live(tmp_path, page)

    assert result.status == "unknown"
    assert result.submitted is False
    assert "Navigation interrupted" in (result.error or "")


def test_greenhouse_confirmation_text_present_before_submit_is_not_trusted(
    tmp_path: Path,
) -> None:
    page = complete_live_page()
    page.visible_text.append("Thank you for your application to Example Co.")

    result = run_live(tmp_path, page)

    assert page.submitted is True
    assert result.status == "unknown"


def test_dry_run_records_why_fields_were_skipped_or_left_blank(tmp_path: Path) -> None:
    profile_path, resume_path = create_profile_and_resume(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Email", field_type="email"),
            FakeField("Resume/CV", field_type="file"),
            FakeField("Years of Rust experience"),
            FakeField("I agree to the terms", field_type="checkbox"),
        ]
    )

    result = run_greenhouse_dry_run(
        page,
        make_job(),
        profile_path,
        resume_path,
        tmp_path / "filled.png",
        answers_client=mock_answer_client(None, None),
        resume_text="Python developer.",
    )

    assert result.status == "manual_review"
    assert result.job_description == "Build software."
    assert result.field_notes["Phone"] == "Skipped: no matching field on this form."
    assert result.field_notes["GitHub URL"] == "Skipped: no matching field on this form."
    assert "do not provide a supported answer" in result.field_notes["Years of Rust experience"]
    assert result.field_notes["I agree to the terms"] == (
        "Unsupported control type (input/checkbox)."
    )
