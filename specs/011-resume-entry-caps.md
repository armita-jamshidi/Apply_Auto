# 011: At most four jobs and three projects per resume

Status: in progress

## Problem

A tailored resume could list five or six projects, each with a single bullet. That reads as
thin: a recruiter sees many entries and little about any of them. Two things caused it. The
tailor could pick up to six entries of any kind, and GitHub repositories (spec 010) added
more projects to choose from. Then, to fit one page, trimming took bullets away until every
entry had one before it dropped any entry.

## Expected behavior

- **Caps.** A tailored resume has at most 4 jobs (kind `experience`) and at most 3 projects
  (kind `project`). The overall limit of 6 entries still applies. Activities and leadership
  are not capped by these limits.
- **Most relevant first.** The tailor lists entries most relevant first. Code keeps the
  first 4 jobs and the first 3 projects in that order and leaves out the rest, with a note
  in the keyword report ("Left out Project 4: the resume already has 3 projects.").
- **GitHub repositories are projects.** An entry from a GitHub repository (`github:` doc id)
  is always a project, whatever kind the model gave it, so it counts against the cap.
- **Fuller bullets.** The prompt states the caps and asks for 3-4 bullets per entry, and to
  leave out a weak entry rather than keep it thin.
- **Trimming keeps two bullets.** To fit one page, bullets are trimmed only from entries
  with more than two. After that, the lowest-value entry is dropped. Only when one entry is
  left does it go below two bullets.

## How to test

`tests/test_entry_caps.py`, offline with fictional entries:

- a draft with 6 jobs and 5 projects keeps the first 4 jobs and the first 3 projects, and
  notes the ones left out;
- activities are kept beside 4 jobs and 3 projects;
- the tailor prompt states both caps;
- a GitHub repository the model calls `experience` is a project in the pool;
- trimming a resume with 3, 2, and 2 bullets takes one from the first entry, then drops the
  last entry, leaving two entries with two bullets each;
- a single entry can still be trimmed to one bullet.
