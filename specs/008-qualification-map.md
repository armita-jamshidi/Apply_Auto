# 008: Qualification map for every job

Status: in progress

## Problem

The resume sub-agent pulls short keyword phrases from a posting's required and preferred
qualifications and writes bullets that use them, but the candidate cannot see which
qualification each bullet answers. The exact qualification lines and what the person would
do in the role are never shown, so checking a tailored resume against the posting means
reading both side by side.

## Expected behavior

For every kit, the resume sub-agent builds a qualification map:

- **Every line, word for word.** Each required qualification, preferred qualification, and
  duty ("what you'll do") is copied from the posting. Code keeps a line only when the
  description contains it (ignoring case, spacing, and a final period), in the
  description's own spelling. Each line gets an id (R1, P1, D1) and up to five terms: the
  words a bullet must use to hit it, each one checked to appear in that line.
- **Bullets aimed at the lines.** Required and preferred terms join the job's keywords, so
  the resume is built, trimmed, and gap-checked against them. The tailor receives every line
  with its terms and is told to write a bullet that hits each one, required lines first.
  A list of skills or technologies the posting asks for counts as required (or preferred).
  When a source names a tool another way ("Postgres" for the posting's "PostgreSQL"), the
  bullet uses the posting's spelling; a short fixed list of same names (`SAME_NAMES`) lets
  that pass the quote check without letting any other new tool through.
  All existing checks stay: every bullet quotes the candidate's documents, and no tool or
  number goes on the resume without a quote.
- **The map is measured in code on the finished resume.** A bullet hits a line when it uses
  at least one of the line's terms as the posting spells them ("Evaluated" does not hit
  "evaluate"). Each match shows the entry, the bullet, the terms hit, and the quotes from
  the candidate's documents it was written from. A bullet trimmed to fit one page is not
  counted. A line with no matching bullet is a gap: it says when the term is only in
  Skills, and offers the suggested bullet for the missing keyword, if one exists, which
  stays off the resume until the candidate confirms it (spec 006).
- **Where it shows.** `kits/job-<id>/qualifications.md` (a Markdown table: id, type, the
  posting's line, the source, the bullet, the terms hit), and the Apply sidebar's
  "How your resume hits each qualification" section. `job-kit` prints how many lines are
  hit. The kit stores the map as JSON in the "Qualification map" note.
- Private: the map holds resume text, so it lives only in `kits/` and the database, both
  git-ignored, and is never logged.

## How to test

Offline tests in `tests/test_qualification_map.py` with a fictional posting and candidate:

- lines not in the posting are dropped, terms not in their line are dropped, ids are R1, R2,
  P1, D1, and required and preferred terms join the keywords;
- the tailor is sent every line; each line maps to the bullets that use its terms, with the
  entry and source quotes; a missing keyword is a gap with its suggestion, kept off the
  resume; an inflected word does not count as a hit;
- a bullet removed from the resume no longer counts, and a term only in Skills says so;
- a source saying "Postgres" supports a bullet saying "PostgreSQL", but not one adding
  Kubernetes;
- the Markdown table lists every line, escapes `|`, and counts required lines hit;
- `build_kit` saves `qualifications.md` and the note, and the dashboard shows the map in the
  Apply panel and not as a form question.
