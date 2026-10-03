"""Mocked tests for non-Greenhouse Tier 1 browser appliers."""

import logging
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.answers import AnswerDecision
from agent.applier.base import DESCRIPTION_SELECTORS, application_urls, run_tier1_dry_run
from agent.types import JobListing


class FakeField:
    def __init__(
        self, label: str, kind: str = "text", group: str | None = None, linked: bool = True
    ) -> None:
        self.label = label
        self.kind = kind
        self.group = group
        self.linked = linked
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
        if "input.labels" in expression:
            return self.label
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
        self.visited: list[str] = []
        self.apply_links = [
            "https://www.smartr.me/oneclick-ui/company/Sample/publication/elsewhere",
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Sample/publication/abc",
        ]
        self.iframes: list[str] = []
        self.form_selectors = {"form", "[role='form']", "main", "body"}
        self.form_lookups: list[str] = []

    @property
    def first(self) -> "FakePage":
        return self

    def goto(self, url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"
        self.visited.append(url)

    def wait_for_load_state(self, _state: str, *, timeout: int) -> None:
        assert timeout > 0

    def locator(self, selector: str):
        if selector in self.sections:
            text = self.sections[selector]
            return SimpleNamespace(count=lambda: 1, first=SimpleNamespace(inner_text=lambda: text))
        if any(selector in selectors for selectors in DESCRIPTION_SELECTORS.values()):
            return SimpleNamespace(count=lambda: 0)
        if selector == 'input[type="file"]':
            files = [item for item in self.fields.values() if item.kind == "file"]
            return SimpleNamespace(count=lambda: len(files), nth=lambda index: files[index])
        if selector in {"form", "[role='form']", "main", "body"}:
            self.form_lookups.append(selector)
            present = selector in self.form_selectors
            return SimpleNamespace(
                count=lambda: 1 if present else 0,
                first=self,
                evaluate=lambda _expression: True,
            )
        if selector == "label":
            fields = list(self.fields.values())
            return SimpleNamespace(
                count=lambda: len(fields),
                nth=lambda index: SimpleNamespace(
                    inner_text=lambda: fields[index].label,
                    evaluate=lambda _script: {
                        "text": fields[index].label,
                        "kind": fields[index].kind if fields[index].linked else None,
                        "group": fields[index].group,
                    },
                ),
            )
        if selector == "input, textarea, select":
            return SimpleNamespace(count=lambda: len(self.fields))
        if selector == "iframe":
            return SimpleNamespace(
                count=lambda: len(self.iframes),
                nth=lambda index: SimpleNamespace(
                    get_attribute=lambda _name: self.iframes[index]
                ),
            )
        if selector == 'a[href*="/oneclick-ui/"]':
            return SimpleNamespace(
                count=lambda: len(self.apply_links),
                nth=lambda index: SimpleNamespace(
                    get_attribute=lambda _name: self.apply_links[index]
                ),
            )
        raise AssertionError(f"Unexpected selector: {selector}")

    def get_by_label(self, label: str, *, exact: bool):
        field = self.fields.get(label)
        return field if field is not None and field.linked else SimpleNamespace(count=lambda: 0)

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


def test_tier1_records_notes_for_blank_and_skipped_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_job_context(monkeypatch)
    profile, resume = create_profile(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Resume", kind="file"),
            FakeField("Why do you want to work here?"),
            FakeField("Required unsupported", kind="checkbox"),
        ]
    )

    result = run_tier1_dry_run(
        page,
        create_job("ashby", "https://jobs.ashbyhq.com/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert result.field_notes["Email"] == "Skipped: no matching field on this form."
    assert result.field_notes["Why do you want to work here?"] == "No grounded answer was found."
    assert result.field_notes["Required unsupported"] == (
        "Checkbox or radio choices are not automated; choose manually."
    )
    assert result.job_description == "Python role."


@pytest.mark.parametrize(
    ("platform", "url", "expected"),
    [
        (
            "lever",
            "https://jobs.lever.co/sample/job-1",
            ("https://jobs.lever.co/sample/job-1", "https://jobs.lever.co/sample/job-1/apply"),
        ),
        (
            "lever",
            "https://jobs.lever.co/sample/job-1/apply?lever-source=board",
            (
                "https://jobs.lever.co/sample/job-1",
                "https://jobs.lever.co/sample/job-1/apply?lever-source=board",
            ),
        ),
        (
            "ashby",
            "https://jobs.ashbyhq.com/sample/job-1/",
            (
                "https://jobs.ashbyhq.com/sample/job-1/",
                "https://jobs.ashbyhq.com/sample/job-1/application",
            ),
        ),
        (
            "ashby",
            "https://jobs.ashbyhq.com/sample/job-1/application",
            (
                "https://jobs.ashbyhq.com/sample/job-1",
                "https://jobs.ashbyhq.com/sample/job-1/application",
            ),
        ),
        (
            "smartrecruiters",
            "https://jobs.smartrecruiters.com/Sample/1",
            ("https://jobs.smartrecruiters.com/Sample/1", None),
        ),
        (
            "smartrecruiters",
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Sample/publication/abc",
            (None, "https://jobs.smartrecruiters.com/oneclick-ui/company/Sample/publication/abc"),
        ),
    ],
)
def test_application_urls_map_overview_and_form_pages(
    platform: str, url: str, expected: tuple[str | None, str | None]
) -> None:
    assert application_urls(platform, url) == expected


def test_tier1_with_stored_description_opens_only_the_form(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()

    run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.lever.co/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert page.visited == ["https://jobs.lever.co/sample/job-1/apply"]


def test_tier1_reads_description_on_overview_then_opens_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts = capture_job_context(monkeypatch)
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.fields["Why do you want to work here?"] = FakeField("Why do you want to work here?")
    page.sections[".posting-page .content"] = "Full posting with requirements."
    job = replace(create_job("lever", "https://jobs.lever.co/sample/job-1/apply"), description="")

    result = run_tier1_dry_run(
        page, job, profile, resume, tmp_path / "filled.png", resume_text="Python engineer."
    )

    assert page.visited == [
        "https://jobs.lever.co/sample/job-1",
        "https://jobs.lever.co/sample/job-1/apply",
    ]
    assert result.job_description == "Full posting with requirements."
    assert contexts[0]["description"] == "Full posting with requirements."


def test_smartrecruiters_follows_same_host_apply_link(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()

    run_tier1_dry_run(
        page,
        create_job("smartrecruiters", "https://jobs.smartrecruiters.com/Sample/1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert page.visited == [
        "https://jobs.smartrecruiters.com/Sample/1",
        "https://jobs.smartrecruiters.com/oneclick-ui/company/Sample/publication/abc",
    ]


def test_smartrecruiters_without_apply_link_fails_clearly(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.apply_links = ["https://www.smartr.me/oneclick-ui/company/Sample/publication/x"]

    result = run_tier1_dry_run(
        page,
        create_job("smartrecruiters", "https://jobs.smartrecruiters.com/Sample/1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert result.status == "failed"
    assert "application form link" in (result.error or "")


def test_bot_challenge_stops_before_filling_and_routes_to_manual_review(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = FakePage([])
    page.iframes = ["https://geo.captcha-delivery.com/captcha/?initialCid=abc"]

    result = run_tier1_dry_run(
        page,
        create_job(
            "smartrecruiters",
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Sample/publication/abc",
        ),
        profile,
        resume,
        tmp_path / "blocked.png",
        resume_text="Python engineer.",
        submit_live=True,
    )

    assert result.status == "manual_review"
    assert result.submitted is False
    assert result.answers == {}
    assert "CAPTCHA" in result.field_notes["Application page"]
    assert result.field_notes["Resume"] == "Not attempted: the application page was blocked."
    assert page.screenshot_path == str(tmp_path / "blocked.png")
    assert page.submitted is False


def test_unrelated_iframe_on_a_real_form_is_not_a_challenge(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.iframes = ["https://geo.captcha-delivery.com/captcha/?initialCid=abc"]

    result = run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.lever.co/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert result.status == "dry_run_ready"


def test_ashby_style_page_without_form_uses_body_and_resume_labelled_input(
    tmp_path: Path,
) -> None:
    profile, resume = create_profile(tmp_path)
    page = FakePage(
        [
            FakeField("First Name"),
            FakeField("Last Name"),
            FakeField("Email", kind="email"),
            FakeField("Autofill from resume", kind="file"),
            FakeField("Resume", kind="file"),
        ]
    )
    page.form_selectors = {"body"}

    result = run_tier1_dry_run(
        page,
        create_job("ashby", "https://jobs.ashbyhq.com/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert result.status == "dry_run_ready"
    assert page.form_lookups == ["form", "[role='form']", "main", "body"]
    assert page.fields["Resume"].uploaded == str(resume)
    assert page.fields["Autofill from resume"].uploaded is None
    assert result.resume_uploaded is True


def test_ambiguous_resume_inputs_route_to_manual_review(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.fields["CV (second copy)"] = FakeField("CV (second copy)", kind="file")

    result = run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.lever.co/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert result.status == "manual_review"
    assert result.resume_uploaded is False
    assert "could not tell which one takes the resume" in result.field_notes["Resume"]
    assert all(field.uploaded is None for field in page.fields.values())


def test_missing_resume_input_is_explained(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = FakePage([FakeField("First Name"), FakeField("Last Name")])

    result = run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.lever.co/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    assert result.field_notes["Resume"] == "No file upload control was found."


def test_lever_style_labels_group_choices_and_strip_required_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_job_context(monkeypatch)
    profile, resume = create_profile(tmp_path)
    page = FakePage(
        [
            FakeField("Name"),
            FakeField("Full name \u2731"),
            FakeField("Email \u2731", kind="email"),
            FakeField("Resume", kind="file"),
            FakeField("English (ENG)", kind="checkbox", group="Which languages do you speak?"),
            FakeField("Spanish (SPA)", kind="checkbox", group="Which languages do you speak?"),
            FakeField("Yes", kind="radio", group="Are you authorized to work in the US?"),
            FakeField("No", kind="radio", group="Are you authorized to work in the US?"),
            FakeField("Yes ", kind="radio", group="Will you require sponsorship?"),
            FakeField("Transcript", kind="file"),
            FakeField("Where are you located?", linked=False),
        ]
    )

    result = run_tier1_dry_run(
        page,
        create_job("lever", "https://jobs.lever.co/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    group_note = "Checkbox or radio choices are not automated; choose manually."
    assert result.answers["Which languages do you speak?"] is None
    assert result.field_notes["Which languages do you speak?"] == group_note
    assert result.field_notes["Are you authorized to work in the US?"] == group_note
    assert result.field_notes["Will you require sponsorship?"] == group_note
    assert not {"English (ENG)", "Spanish (SPA)", "Yes", "No"} & set(result.answers)
    assert "Full name \u2731" not in result.answers
    assert "Email \u2731" not in result.answers
    assert "Transcript" not in result.answers
    assert result.field_notes["Transcript"].startswith("Skipped: file uploads")
    assert result.answers["Where are you located?"] is None
    assert "custom widget" in result.field_notes["Where are you located?"]
    assert page.fields["Resume"].uploaded == str(resume)


def test_ashby_choice_heading_and_options_become_one_review_row(tmp_path: Path) -> None:
    profile, resume = create_profile(tmp_path)
    page = create_complete_page()
    page.fields["Race"] = FakeField("Race", kind="choice-group", group="Race", linked=False)
    page.fields["Asian"] = FakeField("Asian", kind="radio", group="Race")
    page.fields["Hispanic or Latino"] = FakeField("Hispanic or Latino", kind="radio", group="Race")

    result = run_tier1_dry_run(
        page,
        create_job("ashby", "https://jobs.ashbyhq.com/sample/job-1"),
        profile,
        resume,
        tmp_path / "filled.png",
        resume_text="Python engineer.",
    )

    race_options = {"Race", "Asian", "Hispanic or Latino"}
    race_rows = [label for label in result.answers if label in race_options]
    assert race_rows == ["Race"]
    assert result.field_notes["Race"] == (
        "Checkbox or radio choices are not automated; choose manually."
    )
