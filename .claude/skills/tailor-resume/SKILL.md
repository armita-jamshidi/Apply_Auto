---
name: tailor-resume
description: Tailor the user's resume to one job in this project using the job description's exact keywords (action verbs and tech stack, shown in use per project), via job-kit --resume-only. Use when asked to tailor, rewrite, or customize the resume for a job.
---

# Tailor the resume to one job

Use the project command; do not write the resume yourself or read the private sources.

1. Job id: `job-kit --list` (or the id the user gave).
2. `job-kit --job-id <id> --no-questions --resume-only`
   - Sources: the resume (`RESUME_PATH`) and every file in `profile/library/` (the long
     experience bank). More material there means better fits.
   - Output: `kits/job-<id>/resume-<company>-<title>-<date>.docx` and `keywords.md`.
   - Every bullet is checked against exact quotes from those sources; a bullet naming a tool
     or number the sources do not support is dropped and the original wording kept.
3. Keywords under "No evidence in your documents": ask the user how they used each one, append
   `## <keyword>\n<keyword>: <their answer>` to `profile/library/keyword_answers.md`, and rerun
   step 2. Do not invent experience.
4. Report the .docx path and the used / not-used keyword lists.

The model is `resume_tailor_model` in `config/settings.yaml` (default `claude-opus-5-5`).
