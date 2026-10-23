# Roadmap

Work top to bottom. Each item gets a spec in `specs/` before code, and one PR.
Move an item to Done when its PR is merged.

## Now

- **Only healthcare and health-tech roles.** Discovery, cleanup, and kit preparation keep
  only roles whose title or description names healthcare or health tech, on top of the
  0 to 2 years filter. Checked in code before any model call (Oct 23, 2026).
  Spec: [specs/012-healthcare-roles.md](specs/012-healthcare-roles.md).
- **At most four jobs and three projects per resume.** The tailored resume keeps only the
  most relevant four jobs and three projects (GitHub repositories included), each with at
  least two bullets, fills the page, and always includes the current job with its pinned
  bullets (Oct 16, 2026).
  Spec: [specs/011-resume-entry-caps.md](specs/011-resume-entry-caps.md).
- **The resume agent finds skill evidence itself.** No console questions about missing
  skills: the agent searches the master CV, the experience bank, and the candidate's public
  GitHub repositories, and treats "LangGraph or LangChain" as one requirement met by either.
  What it cannot find is listed as a gap (Oct 13, 2026).
  Spec: [specs/010-agent-finds-skill-evidence.md](specs/010-agent-finds-skill-evidence.md).
- **Orchestrator agent.** `job-run --agent --goal N`: one agent with a goal, the existing
  modules as tools, memory of past decisions and runs, and a run log with steps, time, and
  token use. Hard filters and budgets stay in code; there is no submit tool (Oct 10, 2026).
  Spec: [specs/009-orchestrator-agent.md](specs/009-orchestrator-agent.md).
- **Qualification map for every job.** For each posting, a table of every required and
  preferred qualification and every duty, word for word, with the resume bullet that hits
  each one, the source it was written from, and the gaps. First step toward making Job
  Agent an agent (Oct 10, 2026).
  Spec: [specs/008-qualification-map.md](specs/008-qualification-map.md).
- **At most three roles per company.** Keep only the top three open matches for each
  company, in the pipeline and on the dashboard.
  Spec: [specs/007-roles-per-company.md](specs/007-roles-per-company.md).
0. **Resume builder from a master CV.** Pick and reword each resume's entries from the
   master CV, and copy the look of the candidate's own one-page resume.
   Spec: [specs/006-resume-builder.md](specs/006-resume-builder.md).
1. **Keep the tailored resume to one page.** Check the Word file's page count after the
   resume sub-agent writes it, and trim the lowest-value bullets until it fits.

## Next

2. **Show RTP or Remote on each job.** A clear label on every dashboard row saying whether
   the role is in the Research Triangle area or US-remote, so it is visible at a glance.
3. **Daily scheduling.** Run discovery, scoring, and kit preparation once a day on the
   candidate's machine, so fresh jobs are ready each morning.

## Later

- Better Research Triangle coverage (Raleigh, Durham, Chapel Hill, Cary, RTP employers).

## Not planned

- LinkedIn job-alert emails, and explaining each job's fit score on the dashboard: the
  candidate does not want them (Oct 6, 2026).

## Done

Phases 1–12 (Oct 1–6, 2026): discovery across boards and remote sources, fit scoring,
board form fillers, live-mode safeguards, hand-off and assist modes, the dashboard,
targeted discovery, the form agent, the source library, the careers agent, fresh postings
first, and the apply kit (tailored Word resume and answers).

- **Answer every question on the application page** (Oct 6, 2026): every form reading
  feeds the kit and the Apply panel, and Greenhouse jobs' questions are read from the
  board before the form is opened.
  Spec: [specs/001-every-application-question.md](specs/001-every-application-question.md).
