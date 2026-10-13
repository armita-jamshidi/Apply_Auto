# 010: The resume agent finds skill evidence itself

Status: in progress

## Problem

When a posting names a skill the resume, master CV, and experience bank never mention,
`job-kit` stopped and asked in the console: "The job asks for 'LangChain' ... How have you
used it?" The candidate may well have used it in a project on GitHub, and finding that is
the agent's job, not the candidate's. A requirement such as "Know LangGraph or LangChain" was
also treated as two separate skills, so the candidate was asked about both even though
either one meets it.

## Expected behavior

- **Nothing is asked by default.** `job-kit`, the dashboard's Apply button, the daily run,
  and the orchestrator never prompt. A keyword with no evidence anywhere is left off the
  resume and listed as a gap in the kit's keyword report and qualification map, with a
  suggested bullet to check. `job-kit --ask` brings the old console questions back.
  `--no-questions` still runs and does nothing new.
- **GitHub is searched.** The GitHub user is `GITHUB_USERNAME`, or the last part of
  `personal.github` in the profile. Each public, non-fork repository (up to 30, most recently
  pushed first) becomes a source document holding its name, description, topics, main
  language, the libraries named in `requirements.txt`, `pyproject.toml`, or `package.json`
  (lowercased, `_` written as `-`, so `langchain_openai` shows LangChain), and the first 4,000
  characters of its README. The repositories that name at least one of the job's technical
  keywords (at most eight, most names first) join the resume's sources, so a repository can
  become a project entry and its bullets quote it like any other document. The result is
  cached for a day in `data/github/` (git-ignored); if GitHub cannot be reached, a stale
  cache or nothing is used and the kit still runs. `GITHUB_TOKEN`, when set, only raises the
  rate limit; private repositories are never read.
- **"X or Y" is one requirement.** The keyword step also returns `alternatives`: groups the
  posting accepts any one of ("LangGraph or LangChain", "PyTorch/TensorFlow", "a framework
  such as LangChain, LlamaIndex, or CrewAI"). Code keeps a group only when every term is in
  the description and all of them, with "or", "/", "either", "one of", "any of", "such as",
  or "e.g.", appear within 160 characters. When the sources mention one term of a group, the
  others are not gaps, not asked about, not listed as unused, and not shown as missing on
  that qualification line. When none is mentioned, the group is one gap ("LangGraph or
  LangChain").
- All existing checks stay: every bullet quotes the candidate's documents, and no tool or
  number goes on the resume without a quote.

## How to test

`tests/test_github_evidence.py`, offline with mocked HTTP and a fake Anthropic client:
dependency files are parsed; the GitHub user comes from the environment or profile; forks
are skipped, documents are cached for a day, and a stale cache is used when GitHub is down;
"or" groups are kept only when the posting offers a choice; finding LangChain in a
repository meets "LangGraph or LangChain"; only repositories naming the job's keywords are
sent to the model; a kit built with no `ask` uses the GitHub project and reports only the
real gap; `job-kit` asks only with `--ask`.
