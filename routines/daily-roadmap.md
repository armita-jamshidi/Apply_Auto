# Daily roadmap routine

You are improving Job Agent in this repository. Read `CLAUDE.md`, `ROADMAP.md`, and
`REVIEW.md` first and follow them.

1. Check open pull requests on this repository that you opened. If one has failing CI or
   unanswered review comments, fix that first and stop there for today.
2. If a PR for the top "Now" item is open and waiting on review, do not start another
   item on the same code; take the next item that does not overlap, or stop.
3. Otherwise take the top roadmap item that has no open PR. If it has no spec in `specs/`,
   write the spec first. If a spec lists suspected causes, confirm them with a test before
   changing code.
4. Make the smallest change that completes the item or a clear part of it. Add offline
   tests. Run `ruff check .` and `pytest`; both must pass.
5. Update the spec's status and `ROADMAP.md`, then open a pull request from a new branch.
   The PR says what changed for the candidate in plain words and what is left.
6. If something needs the candidate's decision, say so at the top of the PR description.

Never merge, never push to `main`, never commit personal data, and never call a real job
board or the Anthropic API from tests.
