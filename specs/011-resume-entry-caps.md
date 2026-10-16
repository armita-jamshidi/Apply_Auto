# 011: Full, capped entries on every tailored resume

Status: in progress

## Problem

A tailored resume could list five or six projects, each with a single bullet. That reads as
thin: a recruiter sees many entries and little about any of them. The tailor could pick up
to six entries of any kind, GitHub repositories (spec 010) added more projects to choose
from, and to fit one page, trimming took bullets away until every entry had one before it
dropped any entry. The candidate also wants the page filled, and their current job always
on the resume with the bullets they always use.

## Expected behavior

- **Caps.** At most 4 jobs (kind `experience`) and 3 projects (kind `project`), within an
  overall limit of 8 entries. Activities and leadership are not capped by these limits.
  The tailor lists entries most relevant first; code keeps the first 4 jobs and 3 projects
  and notes the rest ("Left out Project 4: the resume already has 3 projects.").
- **GitHub repositories are projects.** An entry from a `github:` document is always a
  project, whatever kind the model gave it, so it counts against the cap.
- **At least two bullets.** The prompt asks for 3-4 bullets per entry, never fewer than 2.
  An entry with fewer verified bullets gets the candidate's own wording: facts of that
  entry found word for word in its document, skipping facts a kept bullet was written
  from. An entry that still has fewer than 2 is left out, with a note.
- **The page is filled.** The prompt asks for enough entries and bullets to fill the page.
  When the verified draft fills less than 90% of the page (the cautious estimate, at the
  format's body size), it is sent back once for more bullets or entries, and the fuller
  version is kept when it has more bullets. Trimming to one page is unchanged otherwise.
- **The current job is always on.** An experience entry whose dates end in "Present",
  "Current", or "Now" is pinned. So is every entry named under `resume.always_include` in
  `profile/profile.yaml` (matched by organization or title, ignoring case and
  punctuation). Each pin can list `bullets`: a short phrase from each fact to keep, such as
  "900 interviews". A kept bullet containing the phrase is locked; otherwise the fact
  containing it is added in the candidate's own wording. A phrase or entry that is not
  found is noted in the keyword report. Pinned entries come first and are kept past the
  caps and the overall limit.
- **Trimming.** To fit one page, bullets are trimmed only from entries with more than two,
  never locked ones. Then the lowest-value entry that is not pinned is dropped. One entry
  always stays, and no entry goes below two bullets.

## How to test

`tests/test_entry_caps.py`, offline with fictional entries: the caps and their notes,
activities beside the caps, padding to two bullets from the entry's own facts and leaving
out an entry that cannot reach two, the current job and profile pins, pinned entries kept
past the caps with their bullets locked, the prompt's wording, GitHub repositories as
projects, the send-back when the page is mostly empty, and trimming that keeps two bullets,
pinned entries, and locked bullets. Older resume tests whose fictional entries have one
fact each use the `one_bullet_entries` fixture.
