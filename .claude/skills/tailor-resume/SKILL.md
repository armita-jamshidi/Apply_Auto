---
name: tailor-resume
description: Tailor the user's resume to one job in this project using the job description's exact keywords (action verbs and tech stack, shown in use per project), via job-kit --resume-only. Use when asked to tailor, rewrite, or customize the resume for a job.
---

# Tailor the resume to one job

Use the project command; do not write the resume yourself or read the private sources.

1. Job id: `job-kit --list` (or the id the user gave).
2. `job-kit --job-id <id> --no-questions --resume-only`
   - Sources: the master CV (`MASTER_CV_PATH`, default `profile/master_cv.pdf`), the resume
     (`RESUME_PATH`), and every file in `profile/library/`. More material means better fits.
   - Look: font, text size, and section headings copied from the format example
     (`RESUME_FORMAT_PATH`, default the `RESUME_PATH` resume).
   - Output: `kits/job-<id>/resume-<company>-<title>-<date>.docx`, `keywords.md`, and
     `qualifications.md` (each posting line, the bullet that hits it, and its source).
   - Every bullet is checked against exact quotes from those sources; a bullet naming a tool
     or number the sources do not support is dropped and the original wording kept.
3. Keywords under "No evidence in your documents": `keywords.md` lists a suggested bullet for
   each, on the entry where it fits best. Show them to the user; for each one they confirm
   (or reword), append `## <keyword> (<entry>)\n<keyword>: <bullet>` to
   `profile/library/keyword_answers.md`, and rerun step 2. Never use an unconfirmed one.
4. Report the .docx path, the used / not-used keyword lists, and the qualification lines
   that are gaps in `qualifications.md`.

The model is `resume_tailor_model` in `config/settings.yaml` (default `claude-opus-5-5`).
