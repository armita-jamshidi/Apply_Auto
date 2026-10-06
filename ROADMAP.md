# Roadmap

Work top to bottom. Each item gets a spec in `specs/` before code, and one PR.
Move an item to Done when its PR is merged.

## Now

1. **Answer every question on the application page.** Some questions on a job's form are
   not answered, and not all of them appear in the dashboard's Apply sidebar. Every reading
   of the form now feeds the kit and the sidebar; next, read questions from the board's API
   before the form is opened.
   Spec: [specs/001-every-application-question.md](specs/001-every-application-question.md).

## Next

2. **Keep the tailored resume to one page.** Check the Word file's page count after the
   resume sub-agent writes it, and trim the lowest-value bullets until it fits.
3. **Daily scheduling.** Run discovery, scoring, and kit preparation once a day on the
   candidate's machine, so fresh jobs are ready each morning.
4. **LinkedIn job-alert emails.** Read the candidate's LinkedIn alert emails (not the site)
   as another job source.

## Later

- Better Research Triangle coverage (Raleigh, Durham, Chapel Hill, Cary, RTP employers).
- Show on the dashboard why each job got its fit score.

## Done

Phases 1–12 (Oct 1–6, 2026): discovery across boards and remote sources, fit scoring,
board form fillers, live-mode safeguards, hand-off and assist modes, the dashboard,
targeted discovery, the form agent, the source library, the careers agent, fresh postings
first, and the apply kit (tailored Word resume and answers).
