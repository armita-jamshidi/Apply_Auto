# 007: At most three roles per company

## Problem

Some companies filled the dashboard with many roles (four or more from one company in a
row). The candidate wants only the top three matches for each company.

## Expected behavior

- At most `max_jobs_per_company` open jobs per company (`config/settings.yaml`, now 3).
- The best matches are kept: prepared jobs ("Ready for you") first, then the highest fit
  scores, then the newest. Unscored jobs rank below scored ones.
- Company names match without case, punctuation, or suffixes, so "Acme, Inc." and "acme"
  are one company (`agent/filters.py` `company_key`).
- Only open jobs count (New, Check location, Ready for you). Applied and skipped jobs are
  always shown and never removed.
- Where the limit is applied:
  - `agent/tracking.py` `best_jobs_per_company` ranks one company's jobs and splits them
    into the kept and the extra. Both places below use it, so they always agree.
  - `agent/pipeline.py` `cap_jobs_per_company` runs in `job-run` (and so in `job-daily`)
    after scoring, and marks the extra jobs removed with the reason "Kept the 3 most
    relevant roles at {company}", so discovery does not add them back.
  - `agent/dashboard.py` `dashboard_rows` shows only the best three open jobs per company,
    called from `write_dashboard` with the setting. This covers jobs found by `job-agent`
    alone, or found before the limit existed, until the next `job-run` removes them. When
    the candidate removes one of the three, the next best takes its place.

## How to test

- `tests/test_remote_boards.py`: the cap keeps the prepared job and the two best scores,
  removes the rest with the reason, and leaves applied jobs and other companies alone.
- `tests/test_posted_and_matching.py`: four spellings of one company are capped as one.
- `tests/test_dashboard.py`: the dashboard shows three open roles for a company with five,
  plus its applied and skipped jobs; without a limit every job is shown.
