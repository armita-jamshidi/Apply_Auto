# 013: US companies only, and AI, prompt, and agentic engineering titles

Status: in review

## Problem

The candidate wants jobs only at companies based in the US, or at companies based abroad
that have a US office. A US-remote posting from a company with no US presence could reach
the dashboard, since the location filter only reads the job's location. The candidate also
wants only AI, prompt, and agentic engineering roles: "Prompt Engineer" was dropped (no AI
word in the title list), and scientist and researcher titles were kept.

## Expected behavior

- `discovery.us_companies_only: true` in `config/settings.yaml`. A posting that says the
  company is based abroad ("headquartered in London", "a Berlin-based startup", "HQ:
  Toronto") is removed with "Company is based outside the US with no US office", unless it
  names a US office: "offices in London and New York", "our US office", "a U.S. entity", "a
  New York office".
- A posting that says nothing about where the company is based is kept. A role located in
  North Carolina is at a US office, so it is kept.
- The check is in code, before scoring. It runs at discovery and on every run's cleanup,
  where it also removes prepared jobs. Applied jobs are never touched.
- Titles: `title_keywords` adds "prompt", "prompting", "context engineer", and "context
  engineering", so "Prompt Engineer" and "Context Engineer" are kept. `title_role_keywords`
  drops "scientist" and "researcher", so "LLM Research Scientist" is removed while
  "Research Engineer, LLMs" is kept.

## How to test

Offline tests in `tests/test_us_companies.py`: headquarters phrasings with and without a US
office, HTML, roles in North Carolina, the setting's default, cleanup of a prepared job,
and discovery skipping a London-based company. Title cases are in `tests/test_pipeline.py`.
