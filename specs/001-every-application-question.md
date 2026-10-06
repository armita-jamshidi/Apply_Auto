# 001: Answer every question on the application page

Status: in progress. Causes 1 and 2 are fixed; reading questions before the form is opened is next.

## Problem

The candidate reports that not every question on a job's application page gets an answer,
and that not every question shows up in the dashboard's Apply sidebar. They then have to
write those answers by hand, which is the work the product promises to remove.

## Expected behavior

- For each job, the Apply sidebar lists every question on the role's application form, in
  form order, followed by the common questions (why this company, why this role, a
  technical project) when the form does not already ask them.
- Each question shows one of: an answer, a draft to review, or a short note saying what
  the candidate must supply and why. No question is silently dropped.
- Questions on later form pages, inside iframes, and in custom widgets are included.
- Re-reading the form after a kit was built adds the new questions to the sidebar.

## Causes

1. Confirmed. `_form_answers` in `agent/apply_kit.py` took questions only from the job's
   latest form attempt, so questions an earlier reading found were lost. It now merges every
   attempt. A job whose form was never read still gets only the three common questions; the
   Apply panel now says so.
2. Confirmed. `agent/dashboard.py` showed only the kit once one existed, hiding questions a
   later form reading found. Those questions now appear in the panel, marked to redo the kit.
3. Not a cause for text questions: the fillers record every question they see in
   `answers`. Only upload fields (cover letter, other files) are notes-only, by design.

## Next

Read a job's questions before anyone opens the form, from the board's public API where it
publishes them (Greenhouse returns them with `?questions=true`), so the kit answers them
the first time.

## How to test

- Offline tests with a fictional job and a recorded form that has questions on two pages:
  every question appears in the kit and in the sidebar rows.
- A kit built before a form read, then a form read with a new question: the sidebar shows
  the new question.
- A job with no form read: the sidebar says the form has not been read yet and offers to
  read it, rather than looking complete.
