"""Mocked tests for non-Greenhouse Tier 1 browser appliers."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.applier.base import run_tier1_dry_run
from agent.types import JobListing


class FakeField:
    def __init__(self, label: str, kind: str = "text") -> None:
        self.label = label
        self.kind = kind
        self.value = ""
        self.uploaded: str | None = None

    @property
    def first(self) -> "FakeField":
        return self

    def count(self) -> int:
        return 1

    def get_attribute(self, name: str) -> str | None:
        if name == "type":
            return self.kind
        return None

    def set_input_files(self, path: str) -> None:
        self.uploaded = path

    def evaluate(self, expression: str) -> str | bool:
        if "input.files" in expression:
            return bool(self.uploaded)
        return "input"

    def fill(self, value: str) -> None:
        self.value = value


class FakePage:
    def __init__(self, fields: list[FakeField]) -> None:
        self.fields = {field.label: field for field in fields}
        self.submitted = False
        self.screenshot_path: str | None = None
        self.valid = True

    @property
    def first(self) -> "FakePage":
        return self

    def goto(self, _url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"

    def wait_for_load_state(self, _state: str, *, timeout: int) -> None:
        assert timeout > 0

    def locator(self, selector: str):
        if selector == 'input[type="file"]':
            field = next((item for item in self.fields.values() if item.kind == "file"), None)
            return field or SimpleNamespace(count=lambda: 0)
        if selector in {"form", "[role='form']", "main"}:
            return SimpleNamespace(
                count=lambda: 1,
                first=self,
                evaluate=lambda _expression: True,
            )
        if selector == "label":
            labels = list(self.fields)
            return SimpleNamespace(
                count=lambda: len(labels),
                nth=lambda index: SimpleNamespace(inner_text=lambda: labels[index]),
            )
        if selector == "label":
            return SimpleNamespace(
                count=lambda: len(self.fields),
                nth=lambda index: SimpleNamespace(inner_text=lambda: list(self.fields)[index]),
            )
        raise AssertionError(f"Unexpected selector: {selector}")

    def get_by_label(self, label: str, *, exact: bool):
        field = self.fields.get(label)
        return field or SimpleNamespace(count=lambda: 0)

    def get_by_text(self, text: str, *, exact: bool):
        attached = any(
            field.uploaded and Path(field.uploaded).name == text for field in self.fields.values()
        )

        def wait_for(**_kwargs):
            if not attached:
                raise PlaywrightTimeoutError("File name is not visible")

        return SimpleNamespace(wait_for=wait_for)

    def screenshot(self, *, path: str, full_page: bool) -> None:
        assert full_page
        self.screenshot_path = path
        Path(path).write_bytes(b"fake")

    def evaluate(self, expression: str):
        if "checkValidity" in expression:
            return self.valid
        raise AssertionError(f"Unexpected page evaluate: {expression}")

    def get_by_role(self, role: str, *, name: object):
        if role == "button":
            return SimpleNamespace(count=lambda: 1, click=self._submit)
        return SimpleNamespace(count=lambda: 0)

    def _submit(self) -> None:
        self.submitted = True


def create_profile(tmp_path: Path) -> tuple[Path, Path]:
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "personal:\n  name: Sample Candidate\n  email: sample@example.com\nskills: [Python]\n",
        encoding="utf-8",
    )
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"placeholder")
    return profile, resume


def create_job(platform: str, url: str) -> JobListing:
    return JobListing(
        source=platform,
        platform=platform,
        company="Sample Co",
        title="Engineer",
        url=url,
        location_raw="Remote - US",
        description="Python role.",
    )


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("lever", "https://jobs.lever.co/sample/job-1"),
        ("ashby", "https://jobs.ashbyhq.com/sample/job-1"),
        ("smartrecruiters", "https://jobs.smartrecruiters.com/Sample/1"),
    ],
)
def test_tier1_appliers_default_to_non_submission(
    tmp_path: Path,
    platform: str,
    url: str,
) -> None:
    profile, resume = create_profile(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Email", kind="email"),
            FakeField("Resume", kind="file"),
        ]
    )
    result = run_tier1_dry_run(
        page,
        create_job(platform, url),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert result.status == "dry_run_ready"
    assert page.fields["First Name"].value == "Sample"
    assert result.resume_uploaded is True
    assert page.submitted is False


def test_tier1_applier_rejects_platform_host_mismatch(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = FakePage([])

    result = run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.ashbyhq.com/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert result.status == "failed"
    assert page.screenshot_path is None


def test_tier1_explicit_live_call_submits_only_complete_form(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Email", kind="email"),
            FakeField("Resume", kind="file"),
        ]
    )

    result = run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.lever.co/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
        submit_live=True,
    )

    assert result.status == "applied"
    assert result.submitted is True
    assert page.submitted is True


def test_tier1_live_call_never_submits_manual_review_form(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Resume", kind="file"),
            FakeField("Required unsupported", kind="checkbox"),
        ]
    )

    result = run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.lever.co/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
        submit_live=True,
    )

    assert result.status == "manual_review"
    assert result.submitted is False
    assert page.submitted is False


def test_tier1_live_call_never_submits_browser_invalid_form(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Email", kind="email"),
            FakeField("Resume", kind="file"),
        ]
    )
    page.valid = False
    result = run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.lever.co/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
        submit_live=True,
    )

    assert result.status == "manual_review"
    assert result.submitted is False
    assert page.submitted is False
