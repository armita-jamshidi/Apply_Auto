"""Mocked tests for non-Greenhouse Tier 1 browser appliers."""

import logging
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.answers import AnswerDecision
from agent.applier.base import DESCRIPTION_SELECTORS, run_tier1_dry_run
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
        self.visible_text: list[str] = []
        self.confirmation_text: str | None = "Application submitted!"
        self.click_error: Exception | None = None
        self.sections: dict[str, str] = {}

    @property
    def first(self) -> "FakePage":
        return self

    def goto(self, _url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"

    def wait_for_load_state(self, _state: str, *, timeout: int) -> None:
        assert timeout > 0

    def locator(self, selector: str):
        if selector in self.sections:
            text = self.sections[selector]
            return SimpleNamespace(count=lambda: 1, first=SimpleNamespace(inner_text=lambda: text))
        if any(selector in selectors for selectors in DESCRIPTION_SELECTORS.values()):
            return SimpleNamespace(count=lambda: 0)
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

    def get_by_text(self, text: str | re.Pattern[str], *, exact: bool = False):
        if isinstance(text, re.Pattern):
            def matches() -> list[str]:
                return [item for item in self.visible_text if text.search(item)]

            def wait_for_match(**_kwargs):
                if not matches():
                    raise PlaywrightTimeoutError("Confirmation is not visible")

            first = SimpleNamespace(wait_for=wait_for_match)
            return SimpleNamespace(count=lambda: len(matches()), first=first)

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
        if self.click_error is not None:
            raise self.click_error
        if self.confirmation_text:
            self.visible_text.append(self.confirmation_text)


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


def create_complete_page() -> FakePage:
    return FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Email", kind="email"),
            FakeField("Resume", kind="file"),
        ]
    )


@pytest.mark.parametrize(
    ("platform", "url", "confirmation"),
    [
        ("lever", "https://jobs.lever.co/sample/job-1", "Application submitted!"),
        ("ashby", "https://jobs.ashbyhq.com/sample/job-1", "Thank you for applying."),
        (
            "smartrecruiters",
            "https://jobs.smartrecruiters.com/Sample/1",
            "Your application has been successfully submitted",
        ),
    ],
)
def test_tier1_live_submit_is_applied_only_after_confirmation(
    tmp_path: Path,
    platform: str,
    url: str,
    confirmation: str,
) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.confirmation_text = confirmation

    result = run_tier1_dry_run(
        page,
        create_job(platform, url),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
        submit_live=True,
    )

    assert result.status == "applied"
    assert result.submitted is True


def test_tier1_live_submit_without_confirmation_is_unknown(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.confirmation_text = None

    with caplog.at_level(logging.WARNING):
        result = run_tier1_dry_run(
            page,
            create_job("lever", "https://jobs.lever.co/sample/job-1"),
            profile,
            resume,
            tmp_path / "filled.png",
            resume_text="Python engineer.",
            submit_live=True,
        )

    assert page.submitted is True
    assert result.status == "unknown"
    assert result.submitted is False
    assert "verify manually" in (result.error or "")
    assert "verify manually" in caplog.text


def test_tier1_live_submit_click_error_is_unknown_not_failed(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.click_error = PlaywrightTimeoutError("Navigation interrupted")

    result = run_tier1_dry_run(
        page,
        create_job("ashby", "https://jobs.ashbyhq.com/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
        submit_live=True,
    )

    assert result.status == "unknown"
    assert result.submitted is False
    assert "Navigation interrupted" in (result.error or "")
    assert result.screenshot_path == str(tmp_path / "filled.png")


def test_tier1_confirmation_text_present_before_submit_is_not_trusted(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.visible_text.append("Thanks for applying! We review every application.")

    result = run_tier1_dry_run(
        page,
        create_job("smartrecruiters", "https://jobs.smartrecruiters.com/Sample/1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
        submit_live=True,
    )

    assert page.submitted is True
    assert result.status == "unknown"


def capture_job_context(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    contexts: list[dict[str, str]] = []

    def fake_answer(question, profile, resume, *, client, job_context):
        contexts.append(dict(job_context))
        return AnswerDecision(answer=None, evidence=None, needs_manual_review=True)

    monkeypatch.setattr("agent.applier.base.answer_custom_question", fake_answer)
    return contexts


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("lever", "https://jobs.lever.co/sample/job-1"),
        ("ashby", "https://jobs.ashbyhq.com/sample/job-1"),
        ("smartrecruiters", "https://jobs.smartrecruiters.com/Sample/1"),
    ],
)
def test_tier1_reads_job_description_from_page_when_not_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    url: str,
) -> None:
    contexts = capture_job_context(monkeypatch)
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.fields["Why do you want to work here?"] = FakeField("Why do you want to work here?")
    page.sections[DESCRIPTION_SELECTORS[platform][-1]] = f"Build {platform} data tools."
    job = replace(create_job(platform, url), description="")

    run_tier1_dry_run(
        page, job, profile, resume, tmp_path / "filled.png", resume_text="Python engineer."
    )

    assert [context["description"] for context in contexts] == [f"Build {platform} data tools."]


def test_tier1_prefers_stored_job_description_over_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts = capture_job_context(monkeypatch)
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.fields["Why do you want to work here?"] = FakeField("Why do you want to work here?")
    page.sections['[data-qa="job-description"]'] = "Page text."

    run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.lever.co/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert contexts[0]["description"] == "Python role."
