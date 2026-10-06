---
name: answer-questions
description: Answer job application questions for one job in this project from the user's profile, resume, experience bank, and writing style, via job-kit. Use when asked to answer application questions, draft "why this company" or "tell us about a technical project", or fill the Apply panel's answers.
---

# Answer application questions for one job

Use the project command; it grounds every answer in the user's sources.

- One or a few questions (prints answers, saves nothing):
  `job-kit --job-id <id> --question "Tell us about a technical project" --question "Why Acme?"`
- Every question for the job, saved to the dashboard's Apply panel:
  `job-kit --job-id <id> --no-questions --answers-only`
  This covers the questions recorded from the job's form plus why-this-company,
  why-this-role, and a technical project.

Get the id from `job-kit --list` if the user named a company instead.

How answers are made (so you can explain results, not redo them):
- Short factual questions: exact phrases from `profile/profile.yaml` and the resume, or
  "needs_you" when no source answers it. Qualification questions are never auto-affirmed.
- Written questions: drafts from `profile/library/`, every sentence quoting a source, written
  in the user's voice from samples in `profile/writing_samples/`, and tied to this company and
  role. Drafts are marked for the user's review.

If answers come back "needs_you" for lack of material, suggest adding a write-up to
`profile/library/` or writing samples to `profile/writing_samples/`, then rerun.
