# 002: One-page resume

Status: done, in review.

## Problem

The tailored resume had no page limit, used Calibri with 9.5 pt contact text, and used
whatever section headings the model chose. The candidate needs a one-page resume that
matches each job's required and preferred qualifications as closely as their sources allow.

## Expected behavior

- One page, Times New Roman, no text smaller than 10 pt.
- Sections in this order: Education, Experience, Technical Projects, Skills.
- The job description's terms from the required qualifications are covered first, then the
  preferred ones, still only where the candidate's documents support them.
- When the content is too long, the body size steps down from 11 to 10.5 to 10 pt, then the
  lowest-value bullets are dropped (bullets with required terms are worth the most). Every
  entry keeps at least one bullet until whole entries must go.
- The keyword report says how many required and preferred terms the resume matched.

## How it works

`write_docx` in `agent/resume_tailor.py` measures the real page count with LibreOffice when
it is installed, and otherwise uses a cautious estimate that may leave a little space at the
bottom of the page. Entries are grouped by kind: projects go under Technical Projects,
everything else under Experience.

## How to test

- A long fictional resume is trimmed to one page and keeps its required-qualification bullet.
- Every run in the Word file is Times New Roman at 10 pt or more, and the four headings appear
  in order.
- With LibreOffice installed, the real layout is one page.
- The keyword report counts required and preferred matches.
