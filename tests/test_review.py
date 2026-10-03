"""Tests for dry-run review pages and the review-related CLI options."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from agent.applier import cli
from agent.applier.greenhouse import ApplierResult
from agent.applier.review import FitSummary, count_statuses, review_rows, write_review_page
from agent.scorer import FitAssessment
from agent.types import JobListing
from db.models import Job

JOB = JobListing(
    source="greenhouse",
    platform="greenhouse",
    company="Example Co",
    title="Software Engineer",
    url="https://boards.greenhouse.io/example/jobs/123",
    location_raw="Remote - US",
    description="Build reliable data tools.",
)


def make_result(**overrides) -> ApplierResult:
    values = {
        "status": "manual_review",
        "answers": {
            "First Name": "Alex",
            "Why do you want to work here?": None,
            "Years of Rust experience": None,
            "Notes": "Line one.\nLine two <b>bold</b>.",
        },
        "screenshot_path": None,
        "tailored_resume_path": None,
        "resume_uploaded": True,
        "suggested_answers": {"Why do you want to work here?": "I build data tools.\nTwice."},
        "field_notes": {
            "Why do you want to work here?": "Grounded draft prepared; review required.",
            "Years of Rust experience": "The profile and resume do not provide an answer.",
            "GitHub URL": "Skipped: no matching field on this form.",
            "Name": "Only one of First Name / Last Name was found.",
        },
        "job_description": "Build reliable data tools.",
    }
    values.update(overrides)
    return ApplierResult(**values)


def test_review_rows_classify_every_field() -> None:
    rows = review_rows(make_result(), "resume.pdf")

    assert [(row.label, row.status) for row in rows] == [
        ("First Name", "filled"),
        ("Why do you want to work here?", "draft"),
        ("Years of Rust experience", "manual_review"),
        ("Notes", "filled"),
        ("Resume", "filled"),
        ("GitHub URL", "skipped"),
        ("Name", "manual_review"),
    ]
    assert rows[1].value == "I build data tools.\nTwice."
    assert count_statuses(rows) == {"filled": 3, "draft": 1, "manual_review": 2, "skipped": 1}


def test_failed_resume_upload_is_manual_review() -> None:
    result = make_result(
        resume_uploaded=False,
        field_notes={"Resume": "Resume upload failed or could not be verified."},
    )

    resume_row = next(row for row in review_rows(result, "resume.pdf") if row.label == "Resume")

    assert resume_row.status == "manual_review"
    assert resume_row.value is None
    assert "could not be verified" in (resume_row.note or "")


def test_review_page_shows_job_fit_and_full_escaped_answers(tmp_path: Path) -> None:
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"png")
    page_path = tmp_path / "reviews" / "review.html"

    write_review_page(
        page_path,
        job=JOB,
        result=make_result(screenshot_path=str(screenshot)),
        fit=FitSummary(
            score=82,
            reasons=["Python data tooling matches the profile."],
            dealbreakers=[],
            recommended_action="apply",
        ),
        resume_name="resume.pdf",
    )

    html = page_path.read_text(encoding="utf-8")
    assert "Software Engineer" in html and "Example Co" in html
    assert "<strong>82</strong>/100" in html
    assert "Python data tooling matches the profile." in html
    assert "Line one.\nLine two &lt;b&gt;bold&lt;/b&gt;." in html
    assert "<b>bold</b>" not in html
    assert "I build data tools.\nTwice." in html
    assert "Skipped: no matching field on this form." in html
    assert screenshot.resolve().as_uri() in html


def test_review_page_explains_missing_fit_score(tmp_path: Path) -> None:
    page_path = tmp_path / "review.html"

    write_review_page(
        page_path,
        job=JOB,
        result=make_result(),
        fit=FitSummary(note="Not scored: no job description was available."),
        resume_name="resume.pdf",
    )

    assert "Not scored: no job description was available." in page_path.read_text(
        encoding="utf-8"
    )


def test_cli_is_headless_unless_headed_is_passed() -> None:
    url = ["--job-url", "https://boards.greenhouse.io/example/jobs/1"]

    assert cli.build_parser().parse_args(url).headed is False
    assert cli.build_parser().parse_args([*url, "--headed"]).headed is True


def test_dry_run_fit_prefers_stored_score(tmp_path: Path) -> None:
    stored = Job(fit_score=77, fit_reasons=["Stored reason."], dealbreakers=[])

    fit = cli._dry_run_fit(stored, "Description.", tmp_path / "unused.yaml")

    assert (fit.score, fit.reasons) == (77, ["Stored reason."])


def test_dry_run_fit_without_description_is_not_scored(tmp_path: Path) -> None:
    fit = cli._dry_run_fit(None, "  ", tmp_path / "unused.yaml")

    assert fit.score is None
    assert "no job description" in (fit.note or "")


def test_dry_run_fit_reports_scoring_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("personal:\n  name: Alex Candidate\n", encoding="utf-8")

    def failing_score(*_args, **_kwargs):
        raise RuntimeError("API key missing")

    monkeypatch.setattr(cli, "score_job", failing_score)

    fit = cli._dry_run_fit(None, "Build tools.", profile)

    assert fit.score is None
    assert "API key missing" in (fit.note or "")


def test_dry_run_fit_scores_description(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("personal:\n  name: Alex Candidate\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "score_job",
        lambda *_args, **_kwargs: FitAssessment(
            score=64, reasons=["Some overlap."], dealbreakers=[], recommended_action="skip"
        ),
    )

    fit = cli._dry_run_fit(None, "Build tools.", profile)

    assert (fit.score, fit.recommended_action) == (64, "skip")


def test_dry_run_writes_review_page_and_runs_headless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launches: list[bool] = []

    class FakePlaywright:
        def __enter__(self):
            def launch(*, headless: bool, chromium_sandbox: bool):
                assert chromium_sandbox is True
                launches.append(headless)
                return SimpleNamespace(new_page=lambda: object(), close=lambda: None)

            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        def __exit__(self, *_exc) -> None:
            return None

    review_path = tmp_path / "review.html"
    monkeypatch.setattr(cli, "sync_playwright", FakePlaywright)
    monkeypatch.setattr(cli, "_load_stored_job", lambda _url: None)
    monkeypatch.setattr(cli, "run_greenhouse_dry_run", lambda *_args, **_kwargs: make_result())
    monkeypatch.setattr(
        cli, "_dry_run_fit", lambda *_args: FitSummary(score=90, reasons=["Strong fit."])
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-apply",
            "--job-url",
            JOB.url,
            "--company",
            "Example Co",
            "--title",
            "Software Engineer",
            "--resume",
            str(tmp_path / "resume.pdf"),
            "--screenshot",
            str(tmp_path / "shot.png"),
            "--review",
            str(review_path),
        ],
    )

    assert cli.main() == 2

    assert launches == [True]
    html = review_path.read_text(encoding="utf-8")
    assert "Strong fit." in html
    assert "I build data tools.\nTwice." in html
    output = capsys.readouterr().out
    assert f"Review page: {review_path}" in output
    assert "3 filled, 1 draft, 2 manual_review, 1 skipped" in output


def test_hand_off_and_live_cannot_be_combined() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--job-url", JOB.url, "--hand-off", "--live"])


def test_review_page_links_form_and_reopen_command(tmp_path: Path) -> None:
    page_path = tmp_path / "review.html"
    args = cli.build_parser().parse_args(
        ["--platform", "lever", "--job-url", "https://jobs.lever.co/sample/job-1"]
    )

    write_review_page(
        page_path,
        job=JOB,
        result=make_result(),
        fit=FitSummary(score=70),
        resume_name="resume.pdf",
        form_url=cli.form_url("lever", args.job_url),
        finish_command=cli.finish_command(args, 'Sample "Co"', "Engineer"),
    )

    html = page_path.read_text(encoding="utf-8")
    assert "Finish this application" in html
    assert "https://jobs.lever.co/sample/job-1/apply" in html
    assert (
        "python -m agent.applier.cli --hand-off --platform lever "
        "--job-url &quot;https://jobs.lever.co/sample/job-1&quot; "
        "--company &quot;Sample Co&quot; --title &quot;Engineer&quot;"
    ) in html


def test_hand_off_fills_in_kept_browser_and_waits_for_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[object] = []
    review_path = tmp_path / "review.html"

    class FakeHandOffPage:
        def wait_for_event(self, name: str, *, timeout: int) -> None:
            assert review_path.is_file(), "review page must exist before waiting"
            events.append(("wait", name, timeout))

    page = FakeHandOffPage()

    class FakePlaywright:
        def __enter__(self):
            def launch_persistent_context(
                user_data_dir: str,
                *,
                headless: bool,
                chromium_sandbox: bool,
                channel: str | None = None,
            ):
                assert chromium_sandbox is True
                events.append(("launch", user_data_dir, headless, channel))
                return SimpleNamespace(
                    pages=[page], close=lambda: events.append("closed")
                )

            def launch(**_kwargs):
                raise AssertionError("hand-off must use the persistent browser profile")

            return SimpleNamespace(
                chromium=SimpleNamespace(
                    launch=launch, launch_persistent_context=launch_persistent_context
                )
            )

        def __exit__(self, *_exc) -> None:
            return None

    filler_kwargs: dict[str, object] = {}

    def fake_lever(used_page, *_args, **kwargs):
        assert used_page is page
        filler_kwargs.update(kwargs)
        return make_result()

    monkeypatch.setattr(cli, "sync_playwright", FakePlaywright)
    monkeypatch.setattr(cli, "BROWSER_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setitem(cli.APPLIERS, "lever", fake_lever)
    monkeypatch.setattr(cli, "_load_stored_job", lambda _url: None)
    monkeypatch.setattr(cli, "_dry_run_fit", lambda *_args: FitSummary(score=80))
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-apply",
            "--hand-off",
            "--platform",
            "lever",
            "--job-url",
            "https://jobs.lever.co/sample/job-1",
            "--resume",
            str(tmp_path / "resume.pdf"),
            "--screenshot",
            str(tmp_path / "shot.png"),
            "--review",
            str(review_path),
        ],
    )

    cli.main()

    assert events == [
        ("launch", str((tmp_path / "profile").resolve()), False, "chrome"),
        ("wait", "close", 0),
        "closed",
    ]
    assert filler_kwargs["submit_live"] is False
    assert filler_kwargs["human_challenge_wait_ms"] == cli.HUMAN_CHALLENGE_WAIT_MS
    assert opened == [review_path.resolve().as_uri()]
    output = capsys.readouterr().out
    assert "submit it yourself" in output
    assert "Mode: hand_off" in output
    assert output.count("Review page:") == 1


class FakeChromium:
    def __init__(self, chrome_installed: bool = True) -> None:
        self.calls: list[str | None] = []
        self.chrome_installed = chrome_installed

    def launch_persistent_context(
        self,
        user_data_dir: str,
        *,
        headless: bool,
        chromium_sandbox: bool,
        channel: str | None = None,
    ):
        assert headless is False
        assert chromium_sandbox is True, "Chrome's sandbox must stay on"
        self.calls.append(channel)
        if channel == "chrome" and not self.chrome_installed:
            raise PlaywrightError("Chromium distribution 'chrome' is not found")
        return SimpleNamespace(user_data_dir=user_data_dir)


def test_hand_off_uses_installed_chrome_by_default(tmp_path: Path) -> None:
    chromium = FakeChromium()

    context = cli.launch_hand_off_browser(
        SimpleNamespace(chromium=chromium), "chrome", tmp_path / "profile"
    )

    assert chromium.calls == ["chrome"]
    assert context.user_data_dir == str((tmp_path / "profile").resolve())


def test_hand_off_falls_back_to_bundled_chromium_without_chrome(tmp_path: Path) -> None:
    chromium = FakeChromium(chrome_installed=False)

    cli.launch_hand_off_browser(SimpleNamespace(chromium=chromium), "chrome", tmp_path / "p")

    assert chromium.calls == ["chrome", None]


def test_browser_chromium_skips_installed_chrome(tmp_path: Path) -> None:
    chromium = FakeChromium()

    cli.launch_hand_off_browser(SimpleNamespace(chromium=chromium), "chromium", tmp_path / "p")

    assert chromium.calls == [None]


def test_everyday_chrome_profile_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    everyday = tmp_path / "Google" / "Chrome" / "User Data"
    monkeypatch.setattr(cli, "DEFAULT_CHROME_PROFILES", (everyday,))
    chromium = FakeChromium()

    for folder in (everyday, everyday / "Default"):
        with pytest.raises(SystemExit, match="everyday Chrome profile"):
            cli.launch_hand_off_browser(SimpleNamespace(chromium=chromium), "chrome", folder)

    assert chromium.calls == []
    assert cli.is_everyday_chrome_profile(tmp_path / "hand-off") is False


def test_review_page_has_copy_buttons_resume_path_and_assist_command(tmp_path: Path) -> None:
    page_path = tmp_path / "review.html"
    resume = tmp_path / "My Resume.pdf"

    write_review_page(
        page_path,
        job=JOB,
        result=make_result(),
        fit=FitSummary(score=70),
        resume_name=resume.name,
        resume_file=str(resume),
        assist_command=cli.hand_off_command("lever", JOB.url, "Example Co", "Engineer", "assist"),
    )

    html = page_path.read_text(encoding="utf-8")
    assert "data-copy='Alex'" in html
    assert "data-copy='Line one.\nLine two &lt;b&gt;bold&lt;/b&gt;.'" in html
    assert f"data-copy='{resume}'" in html
    assert "python -m agent.applier.cli --assist --platform lever" in html
    assert "navigator.clipboard.writeText" in html


def test_assist_opens_untouched_form_and_review_in_own_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launches: list[bool] = []

    class FakePlaywright:
        def __enter__(self):
            def launch(*, headless: bool, chromium_sandbox: bool):
                assert chromium_sandbox is True
                launches.append(headless)
                return SimpleNamespace(new_page=lambda: object(), close=lambda: None)

            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        def __exit__(self, *_exc) -> None:
            return None

    review_path = tmp_path / "review.html"
    opened: list[str] = []
    filler_kwargs: dict[str, object] = {}

    def fake_lever(*_args, **kwargs):
        filler_kwargs.update(kwargs)
        return make_result()

    monkeypatch.setattr(cli, "sync_playwright", FakePlaywright)
    monkeypatch.setitem(cli.APPLIERS, "lever", fake_lever)
    monkeypatch.setattr(cli, "_load_stored_job", lambda _url: None)
    monkeypatch.setattr(cli, "_dry_run_fit", lambda *_args: FitSummary(score=80))
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-apply",
            "--assist",
            "--headed",
            "--platform",
            "lever",
            "--job-url",
            "https://jobs.lever.co/sample/job-1",
            "--resume",
            str(tmp_path / "resume.pdf"),
            "--screenshot",
            str(tmp_path / "shot.png"),
            "--review",
            str(review_path),
        ],
    )

    cli.main()

    assert launches == [True], "assist always works out answers in a hidden browser"
    assert filler_kwargs["submit_live"] is False
    assert opened == ["https://jobs.lever.co/sample/job-1/apply", review_path.resolve().as_uri()]
    output = capsys.readouterr().out
    assert "Mode: assist" in output
    assert "submit it yourself" in output


def test_assist_cannot_be_combined_with_other_modes() -> None:
    for other in ("--live", "--hand-off"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["--job-url", JOB.url, "--assist", other])
