# 003: Daily scheduling

Status: done, in review.

## Problem

Finding, scoring, and preparing jobs only happened when the candidate ran `job-run` by hand,
and `job-run` read forms but did not build the tailored resume. The candidate wants new jobs
waiting in the dashboard every morning, each with a tailored resume and every application
answer, by 9am Eastern.

## Expected behavior

- One command, `job-daily`, finds and scores new jobs, reads their forms, then builds the
  apply kit (tailored resume and every answer) for each good match that has none yet.
- A job gets a kit when it is open (new or ready for you), in North Carolina or US-remote,
  at or above the fit threshold, with no dealbreakers and no kit yet. Fresh postings come
  first, then the best fit scores, up to `daily_run.kit_limit` kits a morning.
- If finding new jobs fails (a board is offline), the jobs already found still get kits. One
  kit's failure does not stop the others.
- Nobody is at the keyboard, so the resume sub-agent asks no console questions; keywords
  without evidence are listed in the kit for the candidate.
- `job-daily --install` schedules the run on the candidate's computer: Windows Task
  Scheduler (runs a missed start as soon as the computer is on, and wakes it from sleep), or
  cron on macOS and Linux. It starts `start_hours_before` hours before `ready_by`, converted
  from `timezone` to the computer's own clock. `job-daily --uninstall` removes it.
- Each run is logged to `data/daily-run.log` (git-ignored). Nothing is submitted.

## Settings

```yaml
daily_run:
  ready_by: "09:00"
  timezone: America/New_York
  start_hours_before: 2
  kit_limit: 10
```

## Limits

- The computer must be on (or asleep, on Windows) for the run to start. cron skips a missed
  run; Task Scheduler runs it when the computer is next on.
- The start time is converted once, at install. If the computer's zone differs from
  `timezone`, run `job-daily --install` again after a daylight-saving change.

## How to test

Offline tests in `tests/test_daily.py`: which jobs get kits and in what order, a failed kit
not stopping the rest, the start time in several zones, the Task Scheduler definition, cron
install and uninstall keeping other lines, and kits still built when discovery fails.
