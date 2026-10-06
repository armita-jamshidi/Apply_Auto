# Demo

How to show the product without exposing anyone's personal data. Use only the fictional
profile in `profile/profile.example.yaml` (Jordan Example).

## Run it

1. Use a separate checkout or move your own `profile/` files aside first.
2. `copy profile\profile.example.yaml profile\profile.yaml` (Windows) or
   `cp profile/profile.example.yaml profile/profile.yaml`.
3. Set `DATABASE_URL=sqlite:///data/demo.db` in `.env` so the demo has its own database.
4. `job-run`, then open `http://127.0.0.1:8765/`.

## Walkthrough

1. The dashboard: fresh AI and agent roles, each with a fit score and the company link.
2. Open a job's details: why it matches and any unmet requirements.
3. Click **Apply**: the role opens on the company site, and the sidebar shows every answer
   with Copy buttons.
4. Download the tailored one-page Word resume and point out the job description's own
   wording in it.

Screenshots for the README may go in this folder only if they show the fictional profile.
