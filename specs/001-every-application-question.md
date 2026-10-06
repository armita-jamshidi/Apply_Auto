# 001: Answer every question on the application page

Status: planned (ROADMAP "Now", item 1)

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

## Suspected causes (confirm before fixing)

1. `agent/apply_kit.py` `_form_answers` takes questions only from the job's latest form
   attempt. A job whose form was never read gets only the three common questions.
2. `agent/dashboard.py` shows `kit or row.latest`: once a kit exists, questions found by a
   later form read are not shown until the kit is rebuilt.
3. Form fillers may record some fields only as notes (`field_notes`) and not as questions,
   so `questions_for_job` never sees them.

## How to test

- Offline tests with a fictional job and a recorded form that has questions on two pages:
  every question appears in the kit and in the sidebar rows.
- A kit built before a form read, then a form read with a new question: the sidebar shows
  the new question.
- A job with no form read: the sidebar says the form has not been read yet and offers to
  read it, rather than looking complete.
