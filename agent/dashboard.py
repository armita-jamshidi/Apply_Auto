"""Local HTML dashboard of discovered jobs, application progress, and what needs you next."""

import argparse
import json
import logging
import re
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from agent.applier.base import application_urls
from agent.applier.greenhouse import ApplierResult
from agent.applier.review import STATUS_LABELS, FieldRow, hand_off_command, review_rows
from agent.settings import PROJECT_ROOT, load_settings
from agent.sources.company_apply import _is_aggregator
from agent.tracking import REMOVED, mark_job
from agent.types import FILLABLE_PLATFORMS, is_fillable
from db.models import Application, Job
from db.session import create_database_engine, create_session_factory, ensure_schema

LOGGER = logging.getLogger(__name__)
DASHBOARD_PATH = PROJECT_ROOT / "dashboard.html"
PROFILE_PATH = PROJECT_ROOT / "profile" / "profile.yaml"
LOCAL_TIMEZONE = ZoneInfo("America/New_York")
REVIEWS_DIR = PROJECT_ROOT / "reviews"
DEFAULT_PORT = 8765
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


def dashboard_rows(session: Session, *, fresh_days: int = 7) -> list[DashboardRow]:
    """Return every job, grouped and ordered: what needs you first, then new by fit.

    Within each group, postings at most fresh_days old come first, so you can apply early.
    """
    fresh_since = datetime.now(UTC) - timedelta(days=fresh_days)
    jobs = session.scalars(
        select(Job).options(selectinload(Job.applications)).where(Job.status != REMOVED)
    ).all()
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
            not _is_fresh(row.job.posted_at, fresh_since),
            -(row.job.fit_score if row.job.fit_score is not None else -1),
            -_aware(row.applied_at or row.job.first_seen_at).timestamp(),
            row.job.company.casefold(),
        ),
    )


def write_dashboard(
    session: Session,
    path: Path | None = None,
    *,
    fit_threshold: int | None = None,
    fresh_days: int | None = None,
) -> list[DashboardRow]:
    """Write the dashboard page and return its rows."""
    path = path or DASHBOARD_PATH
    if fit_threshold is None or fresh_days is None:
        settings = load_settings()
        fit_threshold = settings.fit_score_threshold if fit_threshold is None else fit_threshold
        fresh_days = settings.fresh_posting_days if fresh_days is None else fresh_days
    fresh_since = datetime.now(UTC) - timedelta(days=fresh_days)
    rows = dashboard_rows(session, fresh_days=fresh_days)
    fresh_count = sum(
        _is_fresh(row.job.posted_at, fresh_since) and row.group not in {"applied", "skipped"}
        for row in rows
    )
    details = (
        "".join(
            f"<li><span class='muted'>{escape(label)}</span><pre>{escape(value)}</pre>"
            f"<button type='button' class='copy' data-copy='{escape(value)}'>Copy</button></li>"
            for label, value in _quick_answers()
        )
        or "<li class='muted'>Add your details to profile/profile.yaml.</li>"
    )
    counts = {key: sum(row.group == key for row in rows) for key, _, _ in GROUPS}
    filters = "".join(
        f"<button type='button' data-filter='{key}'>{escape(label)} "
        f"<span class='count'>{counts[key]}</span></button>"
        for key, label, _ in GROUPS
    )
    body = "\n".join(_row_html(row, fit_threshold, fresh_since) for row in rows) or (
        "<tr><td colspan='9' class='muted'>No jobs yet. Run discovery: "
        "<code>python -m agent.main</code></td></tr>"
    )
    stats = (
        ("Jobs found", len(rows)),
        ("Fit scored", sum(row.job.fit_score is not None for row in rows)),
        (
            f"Strong matches ({fit_threshold}+)",
            sum(
                (row.job.fit_score or 0) >= fit_threshold and row.group in {"ready", "new"}
                for row in rows
            ),
        ),
        (f"Posted in the last {fresh_days} days", fresh_count),
        ("Answers ready", sum(_has_answers(row.latest) for row in rows if row.group == "ready")),
        ("Applied", counts["applied"]),
    )
    tiles = "".join(
        f"<div class='tile'><div class='tile-value'>{value}</div>"
        f"<div class='muted'>{escape(label)}</div></div>"
        for label, value in stats
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
.apply-link {{ font-weight: 600; }}
.posted {{ white-space: nowrap; }}
.fresh {{ color: var(--applied); margin-left: 6px; }}
.listed {{ font-size: 0.85rem; }}
details summary {{ cursor: pointer; color: var(--accent); }}
pre {{
  white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0 0; padding: 8px 10px;
  border: 1px solid var(--line); border-radius: 8px; font: 0.8rem ui-monospace, Consolas, monospace;
}}
.help {{ margin-top: 16px; font-size: 0.9rem; }}
.tiles {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px;
  margin-top: 16px;
}}
.tile {{
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 14px; font-size: 0.85rem;
}}
.tile-value {{ font-size: 1.5rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
.match {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
.bar {{
  display: block; width: 64px; height: 6px; margin-top: 4px; border-radius: 3px;
  background: var(--line); overflow: hidden;
}}
.bar span {{ display: block; height: 100%; border-radius: 3px; }}
.strong {{ background: var(--applied); }} .fair {{ background: var(--location); }}
.weak {{ background: var(--skipped); }}
button.toggle {{ padding: 0 10px; font-size: 0.8rem; margin-top: 6px; }}
tr.detail > td {{ border-top: 0; padding-top: 0; }}
.detail-grid {{
  display: grid; grid-template-columns: minmax(220px, 1fr) minmax(0, 2fr); gap: 16px;
  padding: 12px 14px; border: 1px solid var(--line); border-radius: 8px;
}}
@media (max-width: 760px) {{ .detail-grid {{ grid-template-columns: 1fr; }} }}
.detail-grid h3 {{ font-size: 0.9rem; margin: 0 0 6px; }}
.detail-grid ul {{ margin: 0 0 12px; padding-left: 18px; }}
.answers th, .answers td {{ padding: 6px 8px; font-size: 0.875rem; }}
.answers tbody th {{ font-weight: 600; width: 32%; }}
.answers pre {{ margin: 0; padding: 0; border: 0; font: inherit; }}
.f-filled {{ color: var(--applied); }} .f-draft {{ color: var(--location); }}
.f-manual_review {{ color: var(--ready); }} .f-skipped {{ color: var(--skipped); }}
button.copy {{ padding: 0 10px; font-size: 0.75rem; margin-top: 4px; }}
.controls {{ display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; margin-top: 6px; }}
.controls button {{ padding: 2px 12px; font-size: 0.85rem; white-space: nowrap; }}
button.primary {{ background: var(--accent); border-color: var(--accent); color: var(--panel); }}
button.mark:not(.primary) {{ color: var(--applied); border-color: var(--applied); }}
button:disabled {{ opacity: 0.6; cursor: progress; }}
.readonly {{
  margin: 12px 0 0; padding: 10px 14px; border-radius: 10px;
  border: 1px solid var(--location); color: var(--text); background: var(--panel);
}}
button.remove {{ padding: 0 10px; font-size: 0.8rem; color: var(--ready); }}
body.with-panel main {{ margin-right: 440px; }}
#panel {{
  position: fixed; top: 0; right: 0; bottom: 0; width: 420px; max-width: 100vw; z-index: 5;
  overflow-y: auto; background: var(--panel); border-left: 1px solid var(--line);
  padding: 16px 18px 40px; box-shadow: -4px 0 16px rgb(0 0 0 / 0.08);
}}
@media (max-width: 900px) {{ body.with-panel main {{ margin-right: 0; }} }}
#panel h2 {{ font-size: 1.05rem; margin: 0; }}
#panel h3 {{ font-size: 0.9rem; margin: 18px 0 8px; }}
#panel ul {{ list-style: none; padding: 0; margin: 0; }}
#panel li {{ padding: 8px 0; border-top: 1px solid var(--line); }}
#panel li pre {{ margin: 2px 0 0; padding: 0; border: 0; font: inherit; }}
#panel .panel-head {{ display: flex; justify-content: space-between; gap: 8px; }}
#panel textarea {{
  width: 100%; min-height: 70px; font: inherit; padding: 8px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--bg); color: var(--text);
}}
#panel .answers thead {{ display: none; }}
#panel .answers tr {{ display: block; padding: 8px 0; border-top: 1px solid var(--line); }}
#panel .answers th, #panel .answers td {{
  display: block; width: auto; padding: 2px 0; border: 0;
}}
#panel .answers td:empty {{ display: none; }}
.kind {{ font-size: 0.75rem; margin-left: 6px; }}
#toast {{
  position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); max-width: 90vw;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 14px; box-shadow: 0 4px 16px rgb(0 0 0 / 0.15);
}}
#toast button {{ margin-left: 10px; }}
</style>
</head>
<body>
<main>
<h1>Job Dashboard</h1>
<div class="muted">{len(rows)} jobs · updated {escape(generated)}</div>
<div class="tiles">{tiles}</div>
<p id="readonly" class="readonly" hidden><strong>View only.</strong> This page was opened as a
file, so the Mark applied and Remove buttons cannot save. Run <code>job-dashboard</code> and use
the page it opens.</p>
<div class="toolbar">
<button type="button" data-filter="all" aria-pressed="true">All
<span class="count">{len(rows)}</span></button>
{filters}
<button type="button" data-filter="fresh"
title="Posted in the last {fresh_days} days and not yet applied">Fresh
<span class="count">{fresh_count}</span></button>
<input type="search" id="search" placeholder="Search company or role" aria-label="Search jobs">
</div>
<div class="panel table-wrap">
<table>
<thead><tr><th>Status</th><th>Company</th><th>Role</th><th>Level</th><th>Location</th><th>Posted</th>
<th>Match</th>
<th>Last activity</th><th>Next step</th></tr></thead>
<tbody id="jobs">
{body}
</tbody>
</table>
</div>
<div class="help muted">
<p><strong>Match</strong> is the fit score (0-100) from your profile and the job description;
open <strong>Details</strong> for the reasons, unmet requirements, and every prepared answer.
Run <code>job-run</code> to find, score, and prepare answers for new jobs. Nothing is
submitted for you.</p>
<p><strong>Ready for you:</strong> the form was filled; open it with the command under
"Finish", answer what is left, and submit it yourself. When a hand-off window closes, the terminal
asks whether you submitted. Click <strong>Mark applied</strong> once you submit (or
<strong>Undo</strong> to move it back to New), and <strong>Remove</strong> to hide a job you don't
want; removed jobs are not found again.</p>
</div>
</main>
<aside id="panel" hidden aria-label="Apply panel">
<div class="panel-head"><div><h2 id="panel-title"></h2>
<div class="muted" id="panel-company"></div></div>
<button type="button" id="panel-close" aria-label="Close panel">Close</button></div>
<p><a id="panel-apply" target="_blank" rel="noopener">Open the application</a>
<span class="muted"> in your own browser, then copy answers from here, or</span>
<button type="button" id="panel-fill" class="fill">Fill with agent</button></p>
<h3>This application</h3>
<div id="panel-answers"></div>
<h3>Ask about a question</h3>
<textarea id="panel-question" placeholder="Paste a question from the form"></textarea>
<button type="button" id="panel-ask" class="primary">Get answer</button>
<ul id="panel-asked"></ul>
<h3>Your details</h3>
<ul>{details}</ul>
</aside>
<div id="toast" role="status" hidden></div>
<script>
const rows = Array.from(document.querySelectorAll('#jobs tr[data-group]'));
const buttons = Array.from(document.querySelectorAll('button[data-filter]'));
const search = document.getElementById('search');
let active = 'all';
function apply() {{
  const term = search.value.trim().toLowerCase();
  for (const row of rows) {{
    const groupOk = row.dataset.group !== 'removed'
      && (active === 'all' || row.dataset.group === active
        || (active === 'fresh' && row.dataset.fresh === '1'));
    const textOk = !term || row.dataset.search.includes(term);
    row.hidden = !(groupOk && textOk);
    const detail = document.getElementById(row.dataset.detail);
    if (detail) detail.hidden = row.hidden || !row.classList.contains('open');
  }}
}}
document.addEventListener('click', async (event) => {{
  const toggle = event.target.closest('button.toggle');
  if (toggle) {{
    const row = toggle.closest('tr');
    const open = row.classList.toggle('open');
    document.getElementById(row.dataset.detail).hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    toggle.textContent = open ? 'Hide' : 'Details';
    return;
  }}
  const filler = event.target.closest('button.fill');
  if (filler) {{
    const jobId = filler.dataset.job || panelJob;
    if (!LIVE) {{
      notify('Run "job-dashboard" to start the agent from here.');
      return;
    }}
    filler.disabled = true;
    try {{
      const response = await fetch('/jobs/' + jobId + '/fill', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json', 'X-Job-Dashboard': '1' }},
        body: '{{}}',
      }});
      const reply = await response.json();
      if (!response.ok) throw new Error(reply.error || response.statusText);
      notify(reply.message);
    }} catch (error) {{
      notify('Could not start the agent: ' + error.message);
    }} finally {{
      filler.disabled = false;
    }}
    return;
  }}
  const opener = event.target.closest('button.panel-open');
  if (opener) {{
    openPanel(opener.closest('tr'));
    return;
  }}
  const remove = event.target.closest('button.remove');
  if (remove) {{
    const row = remove.closest('tr');
    const saved = await setStatus(
      remove.dataset.job, 'removed', remove.dataset.url, '--mark-removed');
    if (!saved) return;
    row.hidden = true;
    document.getElementById(row.dataset.detail).hidden = true;
    row.dataset.group = 'removed';
    notify('Removed ' + row.cells[1].textContent + ' - ' + row.cells[2].firstChild.textContent,
      async () => {{
        if (await setStatus(remove.dataset.job, remove.dataset.restore)) location.reload();
      }});
    return;
  }}
  const copy = event.target.closest('button.copy');
  if (!copy) return;
  try {{
    await navigator.clipboard.writeText(copy.dataset.copy);
  }} catch {{
    const area = document.createElement('textarea');
    area.value = copy.dataset.copy;
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }}
  copy.textContent = 'Copied';
  setTimeout(() => {{ copy.textContent = 'Copy'; }}, 1500);
}});
for (const button of buttons) {{
  button.addEventListener('click', () => {{
    active = button.dataset.filter;
    for (const other of buttons) other.setAttribute('aria-pressed', String(other === button));
    apply();
  }});
}}
search.addEventListener('input', apply);
const panel = document.getElementById('panel');
let panelJob = null;
function openPanel(row) {{
  panelJob = row.dataset.job;
  document.getElementById('panel-title').textContent = row.dataset.title;
  document.getElementById('panel-company').textContent = row.dataset.company;
  document.getElementById('panel-apply').href = row.dataset.apply;
  document.getElementById('panel-fill').hidden = !row.querySelector('button.fill');
  const answers = document.getElementById('panel-answers');
  answers.replaceChildren();
  const table = document.querySelector('#' + row.dataset.detail + ' table.answers');
  if (table) {{
    answers.append(table.cloneNode(true));
  }} else {{
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = 'No answers prepared yet. Ask about each question below.';
    answers.append(note);
  }}
  document.getElementById('panel-asked').replaceChildren();
  panel.hidden = false;
  document.body.classList.add('with-panel');
}}
document.getElementById('panel-close').addEventListener('click', () => {{
  panel.hidden = true;
  document.body.classList.remove('with-panel');
}});
document.getElementById('panel-ask').addEventListener('click', async () => {{
  const box = document.getElementById('panel-question');
  const question = box.value.trim();
  if (!question || !panelJob) return;
  if (!LIVE) {{
    notify('Run "job-dashboard" to ask questions; this copy cannot reach the answer service.');
    return;
  }}
  const button = document.getElementById('panel-ask');
  button.disabled = true;
  button.textContent = 'Thinking...';
  const item = document.createElement('li');
  try {{
    const response = await fetch('/jobs/' + panelJob + '/answer', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-Job-Dashboard': '1' }},
      body: JSON.stringify({{ question }}),
    }});
    const reply = await response.json();
    if (!response.ok) throw new Error(reply.error || response.statusText);
    const label = document.createElement('div');
    label.textContent = question;
    const kind = document.createElement('span');
    kind.className = 'kind muted';
    const kinds = {{ answer: 'answer', draft: 'draft: review it', none: 'no answer' }};
    kind.textContent = kinds[reply.kind];
    label.append(kind);
    item.append(label);
    if (reply.answer) {{
      const text = document.createElement('pre');
      text.textContent = reply.answer;
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'copy';
      copy.dataset.copy = reply.answer;
      copy.textContent = 'Copy';
      item.append(text, copy);
    }}
    if (reply.note) {{
      const note = document.createElement('div');
      note.className = 'muted';
      note.textContent = reply.note;
      item.append(note);
    }}
    document.getElementById('panel-asked').prepend(item);
    box.value = '';
  }} catch (error) {{
    notify('Could not answer: ' + error.message);
  }} finally {{
    button.disabled = false;
    button.textContent = 'Get answer';
  }}
}});
// Changes are saved by the local server (job-dashboard --serve); an opened file cannot save.
const LIVE = location.protocol === 'http:';
const toast = document.getElementById('toast');
if (LIVE) {{
  for (const link of document.querySelectorAll('a[data-review]')) {{
    link.href = '/reviews/' + encodeURIComponent(link.dataset.review);
  }}
}}
function notify(message, undo) {{
  toast.textContent = message;
  if (undo) {{
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Undo';
    button.addEventListener('click', undo);
    toast.append(button);
  }}
  toast.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => {{ toast.hidden = true; }}, 8000);
}}
async function setStatus(job, status, url, flag) {{
  if (!LIVE) {{
    notify('This copy cannot save. Run "job-dashboard" to change jobs here, or run: '
      + 'python -m agent.dashboard ' + flag + ' "' + url + '"');
    return false;
  }}
  try {{
    const response = await fetch('/jobs/' + job + '/status', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-Job-Dashboard': '1' }},
      body: JSON.stringify({{ status }}),
    }});
    if (!response.ok) throw new Error((await response.json()).error || response.statusText);
    return true;
  }} catch (error) {{
    notify('Could not save: ' + error.message);
    return false;
  }}
}}
for (const mark of document.querySelectorAll('button.mark')) {{
  mark.addEventListener('click', async () => {{
    mark.disabled = true;
    const {{ job, status, url, flag }} = mark.dataset;
    if (await setStatus(job, status, url, flag)) location.reload();
    else mark.disabled = false;
  }});
}}
if (!LIVE) document.getElementById('readonly').hidden = false;
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


def _row_html(row: DashboardRow, fit_threshold: int, fresh_since: datetime) -> str:
    job = row.job
    fresh = _is_fresh(job.posted_at, fresh_since) and row.group not in {"applied", "skipped"}
    listed = ""
    if job.listed_title and job.listed_title != job.title:
        listed = f"<div class='muted listed'>Listed elsewhere as {escape(job.listed_title)}</div>"
    warning = ""
    if job.dealbreakers and row.group != "applied":
        warning = f"<div class='warn'>{escape('; '.join(job.dealbreakers))}</div>"
    when = row.applied_at or (row.latest.started_at if row.latest else job.first_seen_at)
    activity = {
        "applied": "Applied",
        "skipped": "Skipped",
    }.get(row.group, "Filled" if row.latest else "Found")
    apply = application_link(job)
    links = [f"<a href='{escape(apply)}' class='apply-link'>Apply</a>"]
    if apply != job.url:
        links.append(f"<a href='{escape(job.url)}'>Posting</a>")
    if _is_aggregator((urlsplit(apply).hostname or "").casefold()) and not is_fillable(
        job.platform, apply
    ):
        note = (
            "Not on the company's site"
            if job.company_page_checked_at is not None
            else "Company page not looked up yet"
        )
        links.append(f"<div class='muted'>{note}</div>")
    if row.latest and row.latest.review_path:
        review = Path(row.latest.review_path)
        if review.is_file():
            links.append(
                f"<a href='{escape(review.resolve().as_uri())}' "
                f"data-review='{escape(review.name)}'>Review</a>"
            )
    # Undo puts a job back in Ready for you when its form was already filled, else New.
    restore = "manual_review" if any(a.mode != "manual" for a in job.applications) else "new"
    if row.group == "applied":
        applied_button = (
            f"<button type='button' class='mark' data-job='{job.id}' data-status='{restore}' "
            f"data-flag='--mark-new' data-url='{escape(job.url)}' "
            "title='Undo: I have not applied'>Applied ✓ · Undo</button>"
        )
    else:
        applied_button = (
            f"<button type='button' class='mark primary' data-job='{job.id}' "
            f"data-status='applied' data-flag='--mark-applied' data-url='{escape(job.url)}'>"
            "Mark applied</button>"
        )
    fill_button = ""
    if row.group in {"ready", "new", "location"} and fill_command(job) is not None:
        fill_button = (
            f"<button type='button' class='fill' data-job='{job.id}' "
            "title='Opens the application in a new window and lets the agent fill it; "
            "you sign in where asked, review, and submit'>Fill with agent</button>"
        )
    controls = (
        f"<div class='controls'><button type='button' class='panel-open primary' "
        f"data-job='{job.id}'>Apply panel</button>{fill_button}{applied_button}"
        f"<button type='button' class='remove' data-job='{job.id}' "
        f"data-restore='{escape(job.status)}' data-url='{escape(job.url)}'>Remove</button></div>"
    )
    finish = ""
    if row.group in {"ready", "new", "location"}:
        if is_fillable(job.platform, job.url):
            command = hand_off_command(job.platform, job.url, job.company, job.title)
        else:
            # Unknown forms (Workday, company career pages) go to the model-driven form agent.
            command = form_agent_command(apply, job.url)
        finish = f"<details><summary>Finish</summary><pre>{escape(command)}</pre></details>"
    search_text = escape(
        f"{job.company} {job.title} {job.listed_title or ''} {job.location_raw}".casefold()
    )
    detail_id = f"detail-{job.id}"
    fields = _answer_rows(row.latest)
    answered = ""
    if fields:
        filled = sum(field.status == "filled" for field in fields)
        answered = f"<div class='muted'>{filled} of {len(fields)} answered</div>"
    return (
        f"<tr data-group='{row.group}' data-search='{search_text}' data-detail='{detail_id}' "
        f"data-job='{job.id}' data-company='{escape(job.company)}' "
        f"data-title='{escape(job.title)}' data-apply='{escape(apply)}' "
        f"data-fresh='{int(fresh)}'>"
        f"<td><span class='pill {row.group}'>{escape(row.label)}</span></td>"
        f"<td>{escape(job.company)}</td>"
        f"<td>{escape(job.title)}{listed}{warning}"
        "<div><button type='button' class='toggle' aria-expanded='false' "
        f"aria-controls='{detail_id}'>Details</button></div></td>"
        f"<td>{escape(_level(job))}</td>"
        f"<td>{escape(job.location_raw or '')}</td>"
        f"<td>{_posted_html(job.posted_at, fresh)}</td>"
        f"<td>{_match_html(job.fit_score, fit_threshold)}</td>"
        f"<td>{escape(activity)} {escape(_format_date(when))}</td>"
        f"<td class='links'>{''.join(links)}{answered}{controls}{finish}</td>"
        "</tr>\n"
        f"<tr class='detail' id='{detail_id}' hidden><td colspan='9'>"
        f"{_detail_html(row, fields)}</td></tr>"
    )


def fill_command(job: Job) -> list[str] | None:
    """The command that fills a job's real application in a visible browser, if known.

    Supported boards use their own filler; any other company form uses the form agent.
    None when the only link is a job board's page, which is not an application form.
    """
    if is_fillable(job.platform, job.url):
        return [
            sys.executable,
            "-m",
            "agent.applier.cli",
            "--hand-off",
            "--platform",
            job.platform,
            "--job-url",
            job.url,
            "--company",
            job.company,
            "--title",
            job.title,
        ]
    apply = application_link(job)
    host = (urlsplit(apply).hostname or "").casefold()
    if not apply.startswith("https://") or _is_aggregator(host):
        return None
    command = [sys.executable, "-m", "agent.form_agent", "--job-url", apply]
    return command + (["--lookup-url", job.url] if apply != job.url else [])


def launch_in_new_window(command: list[str]) -> None:
    """Start a command in its own console window, so it can ask you to press Enter."""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(command, cwd=PROJECT_ROOT, creationflags=flags)  # noqa: S603


def form_agent_command(apply_url: str, job_url: str) -> str:
    """The command that fills this job's application with the form agent."""
    command = f'python -m agent.form_agent --job-url "{apply_url.replace(chr(34), "")}"'
    if job_url != apply_url:
        command += f' --lookup-url "{job_url.replace(chr(34), "")}"'
    return command


def _quick_answers() -> list[tuple[str, str]]:
    """The candidate's common form answers for the Apply panel; empty without a profile."""
    from agent.applier.greenhouse import load_profile
    from agent.applier.review import profile_quick_answers

    try:
        return profile_quick_answers(load_profile(PROFILE_PATH))
    except (OSError, ValueError) as error:
        LOGGER.info("Apply panel without profile details: %s", error)
        return []


def application_link(job: Job) -> str:
    """The company's own application page when known, else the posting itself."""
    if job.apply_url:
        return job.apply_url
    if job.platform in FILLABLE_PLATFORMS:
        return application_urls(job.platform, job.url)[1] or job.url
    return job.url


def _posted_html(posted_at: datetime | None, fresh: bool = False) -> str:
    if posted_at is None:
        return "<span class='muted'>unknown</span>"
    days = max(0, (datetime.now(UTC) - _aware(posted_at)).days)
    age = "today" if days == 0 else "1 day ago" if days == 1 else f"{days} days ago"
    badge = "<span class='pill fresh'>Fresh</span>" if fresh else ""
    return (
        f"<span class='posted'>{escape(_format_date(posted_at))}</span>{badge}"
        f"<div class='muted'>{age}</div>"
    )


def _is_fresh(posted_at: datetime | None, since: datetime) -> bool:
    return posted_at is not None and _aware(posted_at) >= since


def _match_html(score: int | None, threshold: int) -> str:
    if score is None:
        return "<span class='muted'>not scored</span>"
    strength = "strong" if score >= threshold else "fair" if score >= threshold - 15 else "weak"
    width = max(0, min(score, 100))
    return (
        f"<span class='match'>{score}</span>"
        f"<span class='bar' aria-hidden='true'><span class='{strength}' "
        f"style='width:{width}%'></span></span>"
    )


def _has_answers(application: Application | None) -> bool:
    return bool(application and (application.answers or application.suggested_answers))


def _answer_rows(application: Application | None) -> list[FieldRow]:
    """The latest attempt's fields as its review page shows them, without the resume row."""
    if application is None or not _has_answers(application):
        return []
    result = ApplierResult(
        "dry_run_ready",
        dict(application.answers or {}),
        None,
        None,
        suggested_answers=dict(application.suggested_answers or {}),
        field_notes=dict(application.field_notes or {}),
    )
    return [field for field in review_rows(result, "") if field.label != "Resume"]


def _detail_html(row: DashboardRow, fields: list[FieldRow]) -> str:
    job = row.job
    if job.fit_score is None:
        fit = "<p class='muted'>Not scored yet. Run <code>job-run --skip-discovery</code>.</p>"
    else:
        recommendation = (
            f" · recommended: {escape(job.fit_recommendation)}" if job.fit_recommendation else ""
        )
        fit = f"<p><strong>{job.fit_score}</strong>/100{recommendation}</p>"
        fit += _list_html("Why it matches", job.fit_reasons or [])
        fit += _list_html("Unmet requirements", job.dealbreakers or [], css="warn")
    if row.latest and row.latest.error:
        fit += _list_html("Last attempt", [row.latest.error], css="warn")

    if fields:
        body = "".join(
            "<tr>"
            f"<th scope='row'>{escape(field.label)}</th>"
            f"<td><span class='pill f-{field.status}'>{STATUS_LABELS[field.status]}</span></td>"
            f"<td>{_answer_value(field)}</td>"
            f"<td class='muted'>{escape(field.note or '')}</td>"
            "</tr>"
            for field in fields
        )
        answers = (
            "<div class='table-wrap'><table class='answers'><thead><tr><th>Question</th>"
            "<th>Status</th><th>Answer</th><th>Note</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div>"
        )
    elif row.group in {"applied", "skipped"}:
        answers = "<p class='muted'>No answers were prepared for this job.</p>"
    else:
        answers = (
            "<p class='muted'>No answers prepared yet. <code>job-run --skip-discovery</code> "
            "prepares the best matches, or use the Finish command for this one.</p>"
        )
    return (
        "<div class='detail-grid'>"
        f"<div><h3>Match</h3>{fit}</div>"
        f"<div><h3>Answers</h3>{answers}</div>"
        "</div>"
    )


def _answer_value(field: FieldRow) -> str:
    if field.value is None:
        return "<span class='muted'>blank</span>"
    return (
        f"<pre>{escape(field.value)}</pre>"
        f"<button type='button' class='copy' data-copy='{escape(field.value)}'>Copy</button>"
    )


def _list_html(title: str, items: list[str], css: str = "") -> str:
    if not items:
        return ""
    entries = "".join(f"<li>{escape(item)}</li>" for item in items)
    return f"<h3>{escape(title)}</h3><ul class='{css}'>{entries}</ul>"


def _level(job: Job) -> str:
    labels = {"early": "Early career", "mid": "Mid", "senior": "Senior", "unknown": "Not stated"}
    label = labels.get(job.experience_level or "", "")
    if job.min_years_experience is not None and label:
        label += f" ({job.min_years_experience}+ yrs)"
    return label


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
    mark.add_argument(
        "--mark-removed",
        metavar="JOB_URL",
        help="Hide a job for good; discovery will not add it back (undo with --mark-new)",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open the page")
    parser.add_argument(
        "--file",
        action="store_true",
        help="Only write dashboard.html (view only) instead of serving the page that saves",
    )
    # Serving is the default; --serve is kept so older commands keep working.
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def make_handler(
    session_factory: sessionmaker[Session],
    port: int,
    answer_question: Callable[[Job, str], dict[str, Any]] | None = None,
    launch: Callable[[list[str]], None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Request handler that renders the dashboard and saves status changes."""
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    launch = launch or launch_in_new_window

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            if not self._trusted():
                return
            if self.path in {"/", "/index.html"}:
                with session_factory() as session:
                    write_dashboard(session)
                self._send(200, DASHBOARD_PATH.read_bytes(), "text/html; charset=utf-8")
                return
            match = re.fullmatch(r"/reviews/([\w.-]+\.html)", self.path)
            review = REVIEWS_DIR / match[1] if match else None
            if review is not None and review.is_file():
                self._send(200, review.read_bytes(), "text/html; charset=utf-8")
                return
            self._json(404, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            # Read the body before any reply: answering with unread data left in the socket
            # makes Windows reset the connection, so the browser never sees the response.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b""
            if not self._trusted():
                return
            match = re.fullmatch(r"/jobs/(\d+)/(status|answer|fill)", self.path)
            # A custom header cannot be sent cross-site without a CORS preflight, which this
            # server never approves, so other web pages cannot change your jobs.
            if match is None or self.headers.get("X-Job-Dashboard") != "1":
                self._json(404, {"error": "Not found"})
                return
            try:
                payload = json.loads(body or b"{}")
                status = payload.get("status")
                question = str(payload.get("question") or "").strip()
            except (ValueError, AttributeError):
                self._json(400, {"error": "Expected a JSON object"})
                return
            if match[2] == "answer":
                self._answer(int(match[1]), question)
                return
            if match[2] == "fill":
                self._fill(int(match[1]))
                return
            with session_factory() as session:
                job = session.get(Job, int(match[1]))
                if job is None:
                    self._json(404, {"error": "No such job"})
                    return
                try:
                    mark_job(session, job.url, str(status))
                except ValueError as error:
                    self._json(400, {"error": str(error)})
                    return
                session.commit()
                LOGGER.info("Marked %s - %s as %s", job.company, job.title, status)
            self._json(200, {"status": status})

        def _fill(self, job_id: int) -> None:
            with session_factory() as session:
                job = session.get(Job, job_id)
                command = fill_command(job) if job is not None else None
                if job is not None:
                    title, company = job.title, job.company
            if command is None:
                self._json(400, {"error": "No application form is known for this job yet."})
                return
            try:
                launch(command)
            except OSError as error:
                self._json(500, {"error": f"Could not start the agent: {error}"})
                return
            LOGGER.info("Started the agent on %s - %s", company, title)
            self._json(
                200,
                {
                    "message": "The agent is opening the application in a new window. If it "
                    "asks you to sign in or do a CAPTCHA, do it in that window, then press "
                    "Enter in the agent's console. It never submits; its answers appear here "
                    "when it finishes (reload the page)."
                },
            )

        def _answer(self, job_id: int, question: str) -> None:
            if not question:
                self._json(400, {"error": "Paste a question first."})
                return
            if answer_question is None:
                self._json(503, {"error": "Answering is not available."})
                return
            with session_factory() as session:
                job = session.get(Job, job_id)
                if job is None:
                    self._json(404, {"error": "No such job"})
                    return
                try:
                    reply = answer_question(job, question)
                except Exception as error:  # the answer service is remote; report, don't crash
                    LOGGER.warning("Could not answer %r: %s", question, error)
                    self._json(502, {"error": f"Could not get an answer: {error}"})
                    return
                save_answer(session, job, question, reply)
                session.commit()
            self._json(200, reply)

        def _trusted(self) -> bool:
            # Rejects DNS-rebinding requests that reach this port under another host name.
            if self.headers.get("Host") in allowed_hosts:
                return True
            self._json(403, {"error": "Forbidden"})
            return False

        def _json(self, code: int, payload: dict[str, object]) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return None

    return DashboardHandler


def question_answerer(profile_path: Path | None = None) -> Callable[[Job, str], dict[str, Any]]:
    """Answer a pasted question for a job from the profile, resume, and source library.

    The profile and resume are read on the first question, not when the dashboard starts.
    """
    from agent.answers import answer_custom_question
    from agent.applier.choices import saved_text_answer
    from agent.applier.cli import default_resume_path
    from agent.applier.greenhouse import extract_resume_text, load_profile

    loaded: dict[str, Any] = {}

    def answer(job: Job, question: str) -> dict[str, Any]:
        if not loaded:
            loaded["profile"] = load_profile(
                profile_path or PROJECT_ROOT / "profile" / "profile.yaml"
            )
            loaded["resume"] = extract_resume_text(default_resume_path())
        saved = saved_text_answer(question, loaded["profile"])
        if saved:
            return {"kind": "answer", "answer": saved, "note": "From your saved answers."}
        decision = answer_custom_question(
            question,
            loaded["profile"],
            loaded["resume"],
            job_context={
                "company": job.company,
                "title": job.title,
                "description": job.description,
            },
            long_form=True,
        )
        if decision.answer is None:
            return {
                "kind": "none",
                "answer": None,
                "note": decision.reason or "No grounded answer.",
            }
        kind = "draft" if decision.needs_manual_review else "answer"
        return {
            "kind": kind,
            "answer": decision.answer,
            "note": decision.reason or "",
            "evidence": decision.evidence or "",
        }

    return answer


def save_answer(session: Session, job: Job, question: str, reply: Mapping[str, Any]) -> None:
    """Keep an asked answer with the job's latest attempt (or a new one) for the panel."""
    attempt = session.scalar(
        select(Application)
        .where(Application.job_id == job.id, Application.mode != "live")
        .order_by(Application.started_at.desc(), Application.id.desc())
        .limit(1)
    )
    if attempt is None:
        attempt = Application(job_id=job.id, mode="assist", answers={})
        session.add(attempt)
    answers = dict(attempt.answers or {})
    drafts = dict(attempt.suggested_answers or {})
    notes = dict(attempt.field_notes or {})
    if reply.get("kind") == "draft":
        answers[question] = None
        drafts[question] = str(reply["answer"])
    else:
        answers[question] = reply.get("answer")
    if reply.get("note"):
        notes[question] = str(reply["note"])
    # Reassign so the JSON columns are saved.
    attempt.answers, attempt.suggested_answers, attempt.field_notes = answers, drafts, notes


def serve(port: int, open_browser: bool) -> int:
    """Serve the dashboard on 127.0.0.1 until interrupted."""
    url = f"http://127.0.0.1:{port}/"
    engine = create_database_engine(load_settings().database_url)
    try:
        ensure_schema(engine)
        handler = make_handler(create_session_factory(engine), port, question_answerer())
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            # Usually the dashboard is already running in another terminal: just show it.
            print(f"Dashboard already running at {url} (or pass --port to use another port)")
            if open_browser:
                webbrowser.open(url)
            return 0
        with server:
            print(f"Dashboard: {url} (leave this window open; Ctrl+C to stop)")
            if open_browser:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Apply any requested status change, rewrite the dashboard, and open it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    engine = create_database_engine(load_settings().database_url)
    marked = any((args.mark_applied, args.mark_skipped, args.mark_new, args.mark_removed))
    try:
        ensure_schema(engine)
        with create_session_factory(engine)() as session:
            for status, url in (
                ("applied", args.mark_applied),
                ("skipped", args.mark_skipped),
                ("new", args.mark_new),
                (REMOVED, args.mark_removed),
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
    if args.serve or not (args.file or marked):
        return serve(args.port, open_browser=not args.no_open)
    print(f"Dashboard: {DASHBOARD_PATH} ({len(rows)} jobs)")
    if not args.no_open:
        webbrowser.open(DASHBOARD_PATH.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
