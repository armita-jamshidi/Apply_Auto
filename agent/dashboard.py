"""Local HTML dashboard of discovered jobs, application progress, and what needs you next."""

import argparse
import logging
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from agent.applier.review import hand_off_command
from agent.settings import PROJECT_ROOT, load_settings
from agent.tracking import mark_job
from db.models import Application, Job
from db.session import create_database_engine, create_session_factory, ensure_schema

LOGGER = logging.getLogger(__name__)
DASHBOARD_PATH = PROJECT_ROOT / "dashboard.html"
LOCAL_TIMEZONE = ZoneInfo("America/New_York")
# Display order, label, and job.status values for each dashboard group.
GROUPS = (
    ("ready", "Ready for you", {"manual_review"}),
    ("new", "New", {"new"}),
    ("location", "Check location", {"queued"}),
    ("applied", "Applied", {"applied"}),
    ("skipped", "Skipped", {"skipped"}),
)


@dataclass(frozen=True, slots=True)
class DashboardRow:
    """One job as the dashboard shows it."""

    job: Job
    group: str
    label: str
    latest: Application | None
    applied_at: datetime | None


def dashboard_rows(session: Session) -> list[DashboardRow]:
    """Return every job, grouped and ordered: what needs you first, then new by fit."""
    jobs = session.scalars(select(Job).options(selectinload(Job.applications))).all()
    rows: list[DashboardRow] = []
    for job in jobs:
        group, label = next(
            ((key, name) for key, name, statuses in GROUPS if job.status in statuses),
            ("new", "New"),
        )
        attempts = sorted(job.applications, key=lambda item: (_aware(item.started_at), item.id))
        submitted = [item.submitted_at for item in attempts if item.submitted_at is not None]
        rows.append(
            DashboardRow(
                job=job,
                group=group,
                label=label,
                latest=attempts[-1] if attempts else None,
                applied_at=max(submitted, key=_aware) if submitted else None,
            )
        )
    order = {key: index for index, (key, _, _) in enumerate(GROUPS)}
    return sorted(
        rows,
        key=lambda row: (
            order[row.group],
            -(row.job.fit_score if row.job.fit_score is not None else -1),
            -_aware(row.applied_at or row.job.first_seen_at).timestamp(),
            row.job.company.casefold(),
        ),
    )


def write_dashboard(session: Session, path: Path | None = None) -> list[DashboardRow]:
    """Write the dashboard page and return its rows."""
    path = path or DASHBOARD_PATH
    rows = dashboard_rows(session)
    counts = {key: sum(row.group == key for row in rows) for key, _, _ in GROUPS}
    filters = "".join(
        f"<button type='button' data-filter='{key}'>{escape(label)} "
        f"<span class='count'>{counts[key]}</span></button>"
        for key, label, _ in GROUPS
    )
    body = "\n".join(_row_html(row) for row in rows) or (
        "<tr><td colspan='7' class='muted'>No jobs yet. Run discovery: "
        "<code>python -m agent.main</code></td></tr>"
    )
    generated = datetime.now(LOCAL_TIMEZONE).strftime("%b %d, %Y %I:%M %p")
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Dashboard</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f7f7f5; --panel: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --line: #e2e1dc;
  --accent: #1f5fbf; --ready: #b3261e; --new: #1f5fbf; --location: #8a5a00;
  --applied: #1f7a4d; --skipped: #5f6368;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    color-scheme: dark;
    --bg: #161615; --panel: #1f1f1e; --text: #ecebe6; --muted: #a3a29c; --line: #34332f;
    --accent: #8ab4f8; --ready: #f28b82; --new: #8ab4f8; --location: #f2c14e;
    --applied: #6fcf97; --skipped: #b0b3b8;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 24px 16px 48px; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}}
main {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ font-size: 1.5rem; margin: 0; }}
a {{ color: var(--accent); }}
.muted {{ color: var(--muted); }}
.toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 16px 0; }}
button {{
  font: inherit; padding: 6px 12px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--panel); color: var(--text); cursor: pointer;
}}
button[aria-pressed="true"] {{
  border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent);
}}
.count {{ color: var(--muted); margin-left: 4px; }}
input[type=search] {{
  flex: 1 1 220px; font: inherit; padding: 6px 12px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--panel); color: var(--text);
}}
.panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }}
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{
  text-align: left; vertical-align: top; padding: 10px 12px; border-top: 1px solid var(--line);
}}
thead th {{ border-top: 0; color: var(--muted); font-weight: 600; font-size: 0.85rem; }}
.pill {{
  display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 0.8rem;
  border: 1px solid currentColor; white-space: nowrap;
}}
.ready {{ color: var(--ready); }} .new {{ color: var(--new); }}
.location {{ color: var(--location); }} .applied {{ color: var(--applied); }}
.skipped {{ color: var(--skipped); }}
.warn {{ color: var(--ready); font-size: 0.85rem; }}
.links a {{ margin-right: 10px; white-space: nowrap; }}
details summary {{ cursor: pointer; color: var(--accent); }}
pre {{
  white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0 0; padding: 8px 10px;
  border: 1px solid var(--line); border-radius: 8px; font: 0.8rem ui-monospace, Consolas, monospace;
}}
.help {{ margin-top: 16px; font-size: 0.9rem; }}
</style>
</head>
<body>
<main>
<h1>Job Dashboard</h1>
<div class="muted">{len(rows)} jobs · updated {escape(generated)}</div>
<div class="toolbar">
<button type="button" data-filter="all" aria-pressed="true">All
<span class="count">{len(rows)}</span></button>
{filters}
<input type="search" id="search" placeholder="Search company or role" aria-label="Search jobs">
</div>
<div class="panel table-wrap">
<table>
<thead><tr><th>Status</th><th>Company</th><th>Role</th><th>Location</th><th>Fit</th>
<th>Last activity</th><th>Next step</th></tr></thead>
<tbody id="jobs">
{body}
</tbody>
</table>
</div>
<div class="help muted">
<p><strong>Ready for you:</strong> the form was filled; open it with the command under
"Finish", answer what is left, and submit it yourself. When a hand-off window closes, the terminal
asks whether you submitted. To update a job by hand, run
<code>python -m agent.dashboard --mark-applied "JOB URL"</code> (or <code>--mark-skipped</code>,
<code>--mark-new</code>).</p>
</div>
</main>
<script>
const rows = Array.from(document.querySelectorAll('#jobs tr[data-group]'));
const buttons = Array.from(document.querySelectorAll('button[data-filter]'));
const search = document.getElementById('search');
let active = 'all';
function apply() {{
  const term = search.value.trim().toLowerCase();
  for (const row of rows) {{
    const groupOk = active === 'all' || row.dataset.group === active;
    const textOk = !term || row.dataset.search.includes(term);
    row.hidden = !(groupOk && textOk);
  }}
}}
for (const button of buttons) {{
  button.addEventListener('click', () => {{
    active = button.dataset.filter;
    for (const other of buttons) other.setAttribute('aria-pressed', String(other === button));
    apply();
  }});
}}
search.addEventListener('input', apply);
</script>
</body>
</html>
"""
    path.write_text(page, encoding="utf-8")
    return rows


def refresh_dashboard(path: Path | None = None) -> Path | None:
    """Rewrite the dashboard from the database; returns None when it is unavailable."""
    path = path or DASHBOARD_PATH
    try:
        engine = create_database_engine(load_settings().database_url)
    except (ValueError, SQLAlchemyError, ImportError) as error:
        LOGGER.info("Dashboard not updated; database unavailable: %s", error)
        return None
    try:
        ensure_schema(engine)
        with create_session_factory(engine)() as session:
            write_dashboard(session, path)
        return path
    except SQLAlchemyError as error:
        LOGGER.info("Dashboard not updated; database unavailable: %s", error)
        return None
    finally:
        engine.dispose()


def _row_html(row: DashboardRow) -> str:
    job = row.job
    fit = "" if job.fit_score is None else str(job.fit_score)
    warning = ""
    if job.dealbreakers and row.group != "applied":
        warning = f"<div class='warn'>{escape('; '.join(job.dealbreakers))}</div>"
    when = row.applied_at or (row.latest.started_at if row.latest else job.first_seen_at)
    activity = {
        "applied": "Applied",
        "skipped": "Skipped",
    }.get(row.group, "Filled" if row.latest else "Found")
    links = [f"<a href='{escape(job.url)}'>Posting</a>"]
    if row.latest and row.latest.review_path:
        review = Path(row.latest.review_path)
        if review.is_file():
            links.append(f"<a href='{escape(review.resolve().as_uri())}'>Review</a>")
    finish = ""
    if row.group in {"ready", "new", "location"}:
        command = hand_off_command(job.platform, job.url, job.company, job.title)
        finish = f"<details><summary>Finish</summary><pre>{escape(command)}</pre></details>"
    search_text = escape(f"{job.company} {job.title} {job.location_raw}".casefold())
    return (
        f"<tr data-group='{row.group}' data-search='{search_text}'>"
        f"<td><span class='pill {row.group}'>{escape(row.label)}</span></td>"
        f"<td>{escape(job.company)}</td>"
        f"<td>{escape(job.title)}{warning}</td>"
        f"<td>{escape(job.location_raw or '')}</td>"
        f"<td>{escape(fit)}</td>"
        f"<td>{escape(activity)} {escape(_format_date(when))}</td>"
        f"<td class='links'>{''.join(links)}{finish}</td>"
        "</tr>"
    )


def _format_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return _aware(value).astimezone(LOCAL_TIMEZONE).strftime("%b %d")


def _aware(value: datetime | None) -> datetime:
    # SQLite returns naive datetimes; they are stored in UTC.
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write and open the local job dashboard.")
    mark = parser.add_mutually_exclusive_group()
    mark.add_argument("--mark-applied", metavar="JOB_URL", help="Record that you applied")
    mark.add_argument("--mark-skipped", metavar="JOB_URL", help="Hide a job you won't apply to")
    mark.add_argument("--mark-new", metavar="JOB_URL", help="Move a job back to New")
    parser.add_argument("--no-open", action="store_true", help="Do not open the page")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Apply any requested status change, rewrite the dashboard, and open it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    engine = create_database_engine(load_settings().database_url)
    try:
        ensure_schema(engine)
        with create_session_factory(engine)() as session:
            for status, url in (
                ("applied", args.mark_applied),
                ("skipped", args.mark_skipped),
                ("new", args.mark_new),
            ):
                if url:
                    try:
                        job = mark_job(session, url, status)
                    except ValueError as error:
                        raise SystemExit(str(error)) from error
                    session.commit()
                    print(f"Marked {job.company} - {job.title} as {status}.")
            rows = write_dashboard(session)
    finally:
        engine.dispose()
    print(f"Dashboard: {DASHBOARD_PATH} ({len(rows)} jobs)")
    if not args.no_open:
        webbrowser.open(DASHBOARD_PATH.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
