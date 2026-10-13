---
name: apply-kit
description: Prepare everything to apply to one job in this project - a resume tailored to the job description's exact keywords plus answers to every application question - by running job-kit. Use when asked to prepare, get ready, or make the kit/resume and answers for a job, company, or "the top jobs".
---

# Apply kit for one job

Run the project's commands; do not re-implement or explore the code. Activate the venv
first: `.\.venv312\Scripts\Activate.ps1` (or call `.venv312/Scripts/python.exe -m agent.apply_kit`).

1. Find the job id (skip if the user gave one):
   `job-kit --list` prints `id | fit | kit | company | title`, best fit first. Match the
   company/title the user named.
2. Build the kit: `job-kit --job-id <id>` (it asks nothing; do not pass `--ask`).
   It runs two sub-agents: the answers sub-agent (every question on the job's form plus
   why-this-company, why-this-role, and a technical project) and the resume sub-agent
   (Word resume from the master CV, the resume, `profile/library/`, and the user's public
   GitHub repositories, using the job's exact keywords). It prints how many questions were
   answered, the resume path (`kits/job-<id>/...docx`), and a keyword report.
3. Keywords under "No evidence in your documents" are gaps: no document or GitHub repository
   shows them. When the posting accepts either of two ("LangGraph or LangChain"), one found
   is enough and the gap is listed once. Report gaps; do not ask the user about each one.
   Only if the user volunteers how they used one, append to
   `profile/library/keyword_answers.md`:

   ```
   ## <keyword>
   <keyword>: <the user's answer, in their words>
   ```

   Then rerun only the resume: `job-kit --job-id <id> --resume-only`.
   Never write a keyword answer the user did not give.
4. Tell the user: the resume path, keywords used / not used, and that the dashboard's Apply
   panel (`job-dashboard`, click **Apply** on the job) shows every answer with Copy buttons
   and the resume download.

Exit code 2 means a sub-agent failed; the printed "Problem:" lines say which. Nothing is ever
submitted. Never print or commit files under `profile/` or `kits/`; they are private.
