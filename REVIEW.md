# Review checklist

Every PR must pass these. Reviewers (human or Claude) flag any miss as blocking.

## Privacy (blocking)

- No personal data in the diff: names, contact details, employers, schools, resume text,
  writing samples, salary, target companies. Fixtures use fictional values only.
- Any new file that can hold personal data is git-ignored and listed in
  `scripts/check_private_data.py`, with a test.
- No profile values, answers, or resume text printed to logs that CI or GitHub shows.

## Safety

- Nothing submits an application without the candidate, except `--live` with every
  safeguard passing.
- No account creation, password entry, or CAPTCHA solving. No LinkedIn scraping.
- No invented facts: answers and resume bullets trace to the profile, resume, or library,
  and code (not only the prompt) checks it.

## Correctness

- `ruff check .` and `pytest` pass. New behavior has an offline test (mocked HTTP and SDK).
- Schema changes come with an Alembic migration.
- Failures in one job or one question do not lose the others.

## Scope and clarity

- The PR does one roadmap item and updates its spec and `ROADMAP.md`.
- Dashboard and CLI text is plain and short.
- The README stays accurate when commands or behavior change.
