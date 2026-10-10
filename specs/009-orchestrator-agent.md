# 009: Orchestrator agent (`job-run --agent`)

Status: in progress

## Problem

`job-run` is a fixed script: discover, score up to 150, prepare the top 5, open the
dashboard, always in that order. Nothing looks at a result and decides what to do next. A
run with plenty of good unscored jobs still searches every source first, and a run that
already has five good candidates still scores 150 more. Each run also starts from nothing:
it does not look at what the candidate applied to or removed, or at what earlier runs did.

## Expected behavior

`job-run --agent --goal N` starts one orchestrator agent whose goal is N strong kits
(a tailored resume and every answer, with no problems). The fixed script stays the default.

- **The model picks each step.** Its tools are the existing modules: `get_status`,
  `find_new_jobs` (discovery, company application links, closed postings, filters, the
  per-company limit), `check_company_pages`, `score_jobs`, `list_candidates`,
  `read_application_form` (the headless dry run), `build_kit`, and `finish`.
- **Code decides what is allowed.** The hard filters stay in code. `read_application_form`
  and `build_kit` only accept jobs that `list_candidates` returned in this run (the same
  query as `job-daily`). A job with a kit is not rebuilt, and a form already read is not
  read again. There is no submit tool.
- **Budgets.** One discovery and one company-page check per run, at most `--score-limit`
  scores (default 150), at most 10 form readings, at most 40 tool calls. A spent budget is
  reported to the model as an error, not retried. The run also stops when the goal is met.
- **Memory.** The first message gives the model the current counts, the last 20 jobs the
  candidate applied to, the last 20 jobs the candidate removed on the dashboard (removed
  jobs with no filter reason), and a summary of the last 3 runs.
- **Run log.** Each run is appended to `data/agent-runs.jsonl` (git-ignored and listed in
  `scripts/check_private_data.py`): the goal and budgets, each step's tool, input, time, and
  short result, the orchestrator's own token use (input, output, cache reads and writes,
  request count), the kits ready and with problems, why the run stopped, and the model's
  one-sentence summary. Token use of the sub-agents (scoring, careers agent, resume and
  answers) is not counted yet.
- **Failures.** A step that raises is reported to the model as an error and the run goes on.
- The model is `orchestrator_model` in `config/settings.yaml` (default: `anthropic_model`).
- Console output shows each step and its short result, never profile values or answers.

## How to test

Offline tests in `tests/test_orchestrator.py` with a scripted model and fake tools:

- the agent scores, lists, reads a form, builds two kits, and stops when the goal is met;
  its first message has the status and past decisions;
- `build_kit` and `read_application_form` refuse jobs not listed in this run (filtered,
  unlisted, a non-integer id) and a job that already has a kit;
- discovery runs once, scoring is capped at the budget, form reading stops at its budget,
  and the step limit ends the run;
- a step that raises is reported and the run continues;
- the run log has each step with its time, summed token use, and kits; a kit with problems
  does not count toward the goal; `recent_runs` reads it back;
- memory lists applied jobs and candidate-removed jobs, not filter-removed ones;
- `job-run --agent --goal 3 --score-limit 40` runs the orchestrator with that goal and never
  the fixed script;
- `data/agent-runs.jsonl` is blocked by the private-data check.
