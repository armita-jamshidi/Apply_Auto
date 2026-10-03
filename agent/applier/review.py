"""Local HTML review pages for dry-run application attempts."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Literal

from agent.applier.greenhouse import ApplierResult
from agent.types import JobListing

FieldStatus = Literal["filled", "draft", "manual_review", "skipped"]
STATUS_LABELS: dict[FieldStatus, str] = {
    "filled": "Filled",
    "draft": "Draft, not filled",
    "manual_review": "Manual review",
    "skipped": "Skipped",
}


@dataclass(frozen=True, slots=True)
class FitSummary:
    """Fit score shown on a review page; note explains a missing score."""

    score: int | None = None
    reasons: list[str] = field(default_factory=list)
    dealbreakers: list[str] = field(default_factory=list)
    recommended_action: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class FieldRow:
    """One form field and what the filler did with it."""

    label: str
    status: FieldStatus
    value: str | None
    note: str | None


def hand_off_command(platform: str, job_url: str, company: str, title: str) -> str:
    """Return the command that reopens a job's form, filled, in hand-off mode."""

    def quoted(value: object) -> str:
        return '"' + str(value).replace('"', "") + '"'

    return (
        f"python -m agent.applier.cli --hand-off --platform {platform} "
        f"--job-url {quoted(job_url)} --company {quoted(company)} --title {quoted(title)}"
    )


def review_rows(result: ApplierResult, resume_name: str) -> list[FieldRow]:
    """Order fields as the filler met them, then resume and fields that were never answered."""
    rows: list[FieldRow] = []
    for label, value in result.answers.items():
        note = result.field_notes.get(label)
        if value is not None:
            rows.append(FieldRow(label, "filled", value, note))
        elif label in result.suggested_answers:
            rows.append(FieldRow(label, "draft", result.suggested_answers[label], note))
        else:
            rows.append(FieldRow(label, "manual_review", None, note))
    rows.append(
        FieldRow(
            "Resume",
            "filled" if result.resume_uploaded else "manual_review",
            resume_name if result.resume_uploaded else None,
            result.field_notes.get("Resume"),
        )
    )
    for label, note in result.field_notes.items():
        if label in result.answers or label == "Resume":
            continue
        # Notes for fields the filler never answered are skips unless they flag a problem.
        status: FieldStatus = "skipped" if note.startswith("Skipped") else "manual_review"
        rows.append(FieldRow(label, status, None, note))
    return rows


def count_statuses(rows: list[FieldRow]) -> dict[FieldStatus, int]:
    """Count rows by status in a stable display order."""
    counts = Counter(row.status for row in rows)
    return {status: counts.get(status, 0) for status in STATUS_LABELS}


def write_review_page(
    path: Path,
    *,
    job: JobListing,
    result: ApplierResult,
    fit: FitSummary,
    resume_name: str,
    generated_at: datetime | None = None,
    form_url: str | None = None,
    finish_command: str | None = None,
) -> list[FieldRow]:
    """Write a self-contained HTML review page and return the field rows it shows."""
    rows = review_rows(result, resume_name)
    counts = count_statuses(rows)
    generated = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    description = result.job_description or job.description

    screenshot_html = "<span class='muted'>not captured</span>"
    if result.screenshot_path:
        screenshot = Path(result.screenshot_path).resolve()
        screenshot_html = (
            f"<a href='{escape(screenshot.as_uri())}'>{escape(screenshot.name)}</a>"
        )

    if fit.score is not None:
        fit_html = (
            f"<p class='score'><strong>{fit.score}</strong>/100"
            f"{_recommendation(fit.recommended_action)}</p>"
            f"{_list('Reasons', fit.reasons)}{_list('Dealbreakers', fit.dealbreakers)}"
        )
    else:
        fit_html = f"<p class='muted'>{escape(fit.note or 'Not scored.')}</p>"

    field_rows = "\n".join(
        "<tr>"
        f"<th scope='row'>{escape(row.label)}</th>"
        f"<td><span class='pill {row.status}'>{STATUS_LABELS[row.status]}</span></td>"
        f"<td>{_value(row)}</td>"
        f"<td>{escape(row.note) if row.note else ''}</td>"
        "</tr>"
        for row in rows
    )
    summary = " · ".join(
        f"<span class='pill {status}'>{count} {STATUS_LABELS[status].lower()}</span>"
        for status, count in counts.items()
        if count
    )
    error_html = (
        f"<p class='error'><strong>Error:</strong> {escape(result.error)}</p>"
        if result.error
        else ""
    )
    finish_html = ""
    if form_url or finish_command:
        link_html = (
            f"<p>Application form: <a href='{escape(form_url)}'>{escape(form_url)}</a></p>"
            if form_url
            else ""
        )
        command_html = (
            "<p>To get the form already filled in, in a browser window that stays open for you "
            "to log in, finish, and submit, run this from the project folder:</p>"
            f"<pre class='command'>{escape(finish_command)}</pre>"
            if finish_command
            else ""
        )
        finish_html = (
            f"<section><h2>Finish this application</h2>{link_html}{command_html}</section>"
        )
    description_html = (
        f"<details><summary>Job description</summary><pre>{escape(description)}</pre></details>"
        if description
        else "<p class='muted'>No job description was available.</p>"
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review: {escape(job.title)} at {escape(job.company)}</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f7f7f5; --panel: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --line: #e2e1dc;
  --filled: #1f7a4d; --draft: #8a5a00; --manual: #b3261e; --skipped: #5f6368;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    color-scheme: dark;
    --bg: #161615; --panel: #1f1f1e; --text: #ecebe6; --muted: #a3a29c; --line: #34332f;
    --filled: #6fcf97; --draft: #f2c14e; --manual: #f28b82; --skipped: #b0b3b8;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 24px 16px 48px; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}}
main {{ max-width: 1100px; margin: 0 auto; }}
h1 {{ font-size: 1.5rem; margin: 0 0 4px; }}
h2 {{ font-size: 1.05rem; margin: 0 0 12px; }}
section {{
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px 20px; margin-top: 16px;
}}
dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 12px 0 0; }}
dt {{ color: var(--muted); }} dd {{ margin: 0; overflow-wrap: anywhere; }}
a {{ color: inherit; }}
.muted {{ color: var(--muted); }}
.error {{ color: var(--manual); }}
.score {{ font-size: 1.1rem; margin: 0 0 8px; }}
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{
  text-align: left; vertical-align: top; padding: 10px 8px; border-top: 1px solid var(--line);
}}
thead th {{ border-top: 0; color: var(--muted); font-weight: 600; font-size: 0.85rem; }}
tbody th {{ font-weight: 600; width: 24%; }}
pre {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; }}
.pill {{
  display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 0.8rem;
  border: 1px solid currentColor; white-space: nowrap;
}}
.filled {{ color: var(--filled); }} .draft {{ color: var(--draft); }}
.manual_review {{ color: var(--manual); }} .skipped {{ color: var(--skipped); }}
details pre {{ margin-top: 12px; max-height: 480px; overflow: auto; }}
pre.command {{
  padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px;
  font-family: ui-monospace, Consolas, monospace; font-size: 0.85rem;
}}
</style>
</head>
<body>
<main>
<header>
<h1>{escape(job.title)}</h1>
<div class="muted">{escape(job.company)} · dry run · generated {generated}</div>
<dl>
<dt>Outcome</dt><dd><strong>{escape(result.status)}</strong></dd>
<dt>Job URL</dt><dd><a href="{escape(job.url)}">{escape(job.url)}</a></dd>
<dt>Screenshot</dt><dd>{screenshot_html}</dd>
<dt>Fields</dt><dd>{summary}</dd>
</dl>
{error_html}
</header>
{finish_html}
<section>
<h2>Fit score</h2>
{fit_html}
</section>
<section>
<h2>Fields</h2>
<div class="table-wrap">
<table>
<thead><tr><th>Field</th><th>Status</th><th>Value</th><th>Note</th></tr></thead>
<tbody>
{field_rows}
</tbody>
</table>
</div>
</section>
<section>
{description_html}
</section>
</main>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return rows


def _value(row: FieldRow) -> str:
    if row.value is None:
        return "<span class='muted'>blank</span>"
    return f"<pre>{escape(row.value)}</pre>"


def _recommendation(action: str | None) -> str:
    return f" · recommended: {escape(action)}" if action else ""


def _list(title: str, items: list[str]) -> str:
    if not items:
        return ""
    entries = "".join(f"<li>{escape(item)}</li>" for item in items)
    return f"<h3 class='muted'>{escape(title)}</h3><ul>{entries}</ul>"
