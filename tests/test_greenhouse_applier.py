"""Mocked tests for the Greenhouse dry-run browser workflow."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from agent.applier.greenhouse import load_profile, run_greenhouse_dry_run
from agent.types import JobListing


class FakeField:
    def __init__(
        self,
        label: str,
        tag: str = "input",
        field_type: str = "text",
        role: str | None = None,
    ) -> None:
        self.label = label
        self.tag = tag
        self.field_type = field_type
        self.role = role
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
        return self.field_type if name == "type" else None

    def evaluate(self, _expression: str) -> str:
        return self.tag

    def set_input_files(self, path: str) -> None:
        self.uploaded_file = path


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

    def locator(self, selector: str) -> FakeLabels | FakeField | SimpleNamespace:
        if selector == "#application-form label":
            return FakeLabels(list(self.fields))
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

    def get_by_role(self, role: str, *, name: str, exact: bool) -> SimpleNamespace:
        matches = [
            field
            for field in self.fields.values()
            if field.role == role
            and (field.label == name if exact else name.casefold() in field.label.casefold())
        ]
        return SimpleNamespace(count=lambda: len(matches))

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
            FakeField("Country", tag="input", role="combobox"),
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
