# Job Agent: instructions for Claude

Job Agent is a privacy-conscious job search assistant for one candidate. It finds AI and
agent engineering roles (US-remote or in the Research Triangle area of North Carolina),
scores each against the candidate's profile, tailors a one-page Word resume to each
posting's exact wording, and answers every application question in the candidate's voice.
The local dashboard lists each job with its fit score and a link to the role on the
company's site, and shows every answer in its Apply sidebar.

The buyer and only user is the candidate. The promise is convenience: less time tailoring
resumes, answering questions, and searching.

## Workspace map

| Path | What it holds |
| --- | --- |
| `README.md` | Architecture, commands, safety rules. Read it before changing code. |
| `ROADMAP.md` | What to work on next, in order. |
| `REVIEW.md` | What every change must pass before it is merged. |
| `context/` | Product context that is safe to commit; personal context in `context/private/` (git-ignored). |
| `customers/` | Who the product serves. Only the README and the template are committed. |
| `specs/` | One spec per roadmap item: the problem, the expected behavior, how to test it. |
| `demo/` | How to show the product with fictional data. |
| `routines/` | Prompts for scheduled work, such as the daily roadmap routine. |
| `agent/`, `db/`, `tests/` | The code; see the README's architecture section. |
| `.claude/skills/` | `apply-kit`, `tailor-resume`, `answer-questions` for running the kit. |

## Privacy rules (never break these)

- The repository is public. Personal data never enters Git: no names, contact details,
  employers, schools, resume text, writing samples, salary, or target companies.
- Personal context lives only in git-ignored paths: `profile/profile.yaml`, the resume,
  `profile/library/`, `profile/writing_samples/`, `context/private/`, `customers/*.md`
  (except the template), `kits/`, `reviews/`, the database, and `.env`.
- Tests and examples use fictional values only (for example "Jordan Example").
- `scripts/check_private_data.py` runs as a pre-commit hook. When adding a new private
  path, add it to `.gitignore` and to `PRIVATE_PATTERNS`, with a test.
- Never log or print profile values, answers, or resume text in CI output.

## How to work

- Every change is a branch and a pull request; the candidate merges. Never push to `main`.
- Keep each PR to one roadmap item, with tests. Update the item's spec and `ROADMAP.md`.
- Before pushing: `ruff check .` and `pytest`. Tests are offline: mock HTTP and the
  Anthropic SDK; never call a real API or job board in a test.
- Follow the safety rules in the README: nothing is submitted without the candidate, no
  accounts or CAPTCHAs, no guessed facts, no LinkedIn scraping, respect robots.txt.
- Model output is checked in code (quotes verified, titles seen on opened pages). Keep it
  that way when adding agent behavior.
- Database changes need an Alembic migration in `db/migrations/versions/`.
- Write user-facing text (dashboard, CLI) in plain, short sentences.

## Commands

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
job-run          # full pipeline and dashboard (needs ANTHROPIC_API_KEY and a profile)
job-kit --list   # job ids for the kit commands
```
