# 012: Keep only healthcare and health-tech roles

Status: in review

## Problem

The candidate wants every job Job Agent finds to be in healthcare or health tech, and open
to someone with 1 to 2 years of experience. Discovery kept AI and agent engineering roles in
every industry, so most of the dashboard was roles the candidate would not apply to.

## Expected behavior

- A role is kept only when its title or description names healthcare or health tech: one of
  `discovery.industry_keywords` in `config/settings.yaml` (for example "healthcare",
  "digital health", "clinical", "patients", "EHR", "HIPAA", "life sciences"), matched as
  whole words in any case.
- Benefits sentences do not count. "We offer medical, dental, and vision insurance" says
  nothing about the company's field, so a sentence that also mentions dental, vision,
  insurance, benefits, 401(k), wellness, PTO, parental leave, or perks is skipped.
- The check is in code, before scoring, so no model call is spent on a role outside
  healthcare. It runs at discovery, on every run's cleanup (a prepared role outside
  healthcare is removed with "Not a healthcare or health-tech role"), and again before
  kits are prepared. Applied jobs are never touched.
- A blank description is not judged: some titles are checked before the posting is fetched,
  and the full posting is checked on the next run.
- Years of experience are unchanged from spec 004: a role that requires 3 or more years is
  removed (`max_years_experience: 2`), and a mid-level role must state 2 years or fewer.
  When both apply, the years reason is shown.
- Removing `industry_keywords` from the settings keeps roles in every industry again.

## How to test

Offline tests in `tests/test_industry_filter.py`: healthcare wording in titles, descriptions,
and HTML is kept; retail roles and benefits-only mentions of "medical" or "health care" are
removed; a 1-2 years healthcare role is kept and a 3+ years one is removed; settings with no
or custom keywords; and cleanup removes a prepared non-healthcare role but keeps applied
ones. Existing tests use fictional healthcare descriptions.
