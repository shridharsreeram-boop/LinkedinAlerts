# Job Alert Pipeline 🔔

A free, automated, multi-user job alert system. Subscribers sign up with a
target job title, location, and an alert duration. A scheduled pipeline
checks for new matching job postings, uses Claude to score how relevant each
one actually is, and emails a digest of the best matches — automatically
expiring the subscription once the chosen period ends.

Built entirely on free-tier infrastructure: **GitHub Actions** (compute and
scheduling), **GitHub Pages** (signup page + live dashboard), **Adzuna API**
(job data for ~19 countries), **Sweden's official JobTech/Platsbanken API**
(free, no key required), **Claude API** (relevance scoring), and **Resend**
(email delivery).

🔗 **Live signup page:** https://shridharsreeram-boop.github.io/LinkedinAlerts/
🔗 **Live dashboard:** https://shridharsreeram-boop.github.io/LinkedinAlerts/dashboard.html
🔗 **Repo:** https://github.com/shridharsreeram-boop/LinkedinAlerts

---

## Why this project

Job boards send a firehose of postings, most irrelevant, and most aggregator
sites don't cover Sweden at all. This pipeline:

1. Pulls fresh postings for a specific title + location from **two** sources
   so coverage spans Sweden and ~19 other countries
2. Filters out anything already seen across runs
3. Uses an LLM to judge actual *relevance*, not just keyword overlap
4. Emails only what's worth the subscriber's time
5. Expires automatically — no stale subscriptions sitting around forever

It's also a self-contained demonstration of: dual-API integration, scheduled
automation, AI-assisted filtering, cross-run state management (dedup), a
self-healing signup flow (resubmitting updates your preferences), and a
simple public ops dashboard — built without paying for any infrastructure.

---

## Architecture

```
![Architecture](docs/job_alert_pipeline_architecture.png)
```

### Why two job sources
Adzuna does not support Sweden as a country code at all (supported codes:
`at, au, be, br, ca, ch, de, es, fr, gb, in, it, mx, nl, nz, pl, sg, us, za`).
Rather than work around that gap, the pipeline calls Sweden's own free,
official JobTech/Platsbanken API directly, and runs Adzuna in parallel across
every country it does support. Results from both are merged, deduplicated,
and scored identically.

---

## Setup (step by step)

### 1. Create free accounts / API keys

| Service | Purpose | Free tier | Required? |
|---|---|---|---|
| [Adzuna API](https://developer.adzuna.com/) | Job data, ~19 countries | Free, generous limits | Yes |
| Sweden's [JobTech/Platsbanken API](https://jobtechdev.se) | Job data, Sweden | Free, no signup/key needed | Built in, nothing to set up |
| [Anthropic Console](https://console.anthropic.com/) | Relevance scoring | Pay-as-you-go, **requires adding billing credit** even for a tiny amount — without it every call returns a 400 and the pipeline falls back to a neutral score | Optional but recommended |
| [Resend](https://resend.com/) | Email delivery | 100/day free, but **sandbox sender can only deliver to your own verified account email** until you verify a domain | Yes |
| GitHub | Hosting, scheduling, Pages | Free for public repos | Yes |

### 1b. (Optional) Use the setup wizard instead of manual secret entry

`scripts/setup_wizard.py` automates the credential-validation and
GitHub-secrets part of setup. Run it **locally on your own machine**
(never on a shared server) after installing the
[GitHub CLI](https://cli.github.com) and running `gh auth login`:

```bash
pip install requests
python scripts/setup_wizard.py --repo yourusername/job-alert-pipeline
```

It prompts for each API key with hidden input (nothing written to disk),
validates it with a real test request before accepting it, and pushes it
straight into GitHub Secrets via `gh secret set` — the same client-side
encrypted mechanism GitHub's own docs recommend.

### 2. Create the signup form
- Google Form with fields: **Name, Email, Job Title, Location, Country
  Code, Alert Duration (days)**
- Link it to a Sheet → **File → Share → Publish to web → CSV** → copy the URL
- Set that URL as the `GOOGLE_SHEET_CSV_URL` secret

### 3. Configure GitHub repo secrets
**Settings → Secrets and variables → Actions**:
`ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `ANTHROPIC_API_KEY`, `RESEND_API_KEY`,
`ALERT_FROM_EMAIL`, `GOOGLE_SHEET_CSV_URL`

### 4. Enable GitHub Pages
If your `docs/` folder build fails with a Jekyll/SCSS error, switch
**Settings → Pages → Source** to **GitHub Actions** and use a "Static HTML"
deploy workflow pointing at `./docs` — this avoids Jekyll's default theme
entirely, which otherwise tries (and fails) to compile a stylesheet that
doesn't exist in a plain static site.

### 5. Enable the workflow
Runs automatically every 6 hours via `.github/workflows/job-alert.yml`, or
trigger manually from the **Actions** tab (`workflow_dispatch`).

---

## Known limitations (intentional, documented tradeoffs)

- **Single-recipient email delivery.** Resend's free sandbox sender
  (`onboarding@resend.dev`) can only deliver to the email address that owns
  the Resend account — this is an anti-spam safeguard, not a quota issue.
  Sending to arbitrary subscribers requires verifying a real domain (DNS
  records) in Resend, which costs a few dollars/year for a domain. This repo
  currently runs single-recipient; multi-recipient is a documented next step,
  not a missing feature.
- **Relevance scoring requires Anthropic billing credit.** The API key alone
  isn't enough — Plans & Billing needs an actual balance, or every scoring
  call returns a 400 and the pipeline silently falls back to a flat neutral
  score (still functional, just unscored).
- **Adzuna has no Sweden coverage.** Solved here by adding Sweden's own
  JobTech API as a second source rather than working around the gap with a
  neighboring country code.
- **Google's "publish to web" CSV can lag a few minutes** after a fresh form
  submission. If a sync run shows stale data, it's almost always this caching
  delay, not a code bug — re-running a few minutes later resolves it.

## Local testing

```bash
pip install requests
export ADZUNA_APP_ID=xxx ADZUNA_APP_KEY=xxx ANTHROPIC_API_KEY=xxx RESEND_API_KEY=xxx ALERT_FROM_EMAIL=alerts@yourdomain.com
python scripts/run_pipeline.py
python scripts/generate_dashboard.py
```

## Possible extensions
- Verify a domain in Resend for genuine multi-recipient delivery
- Add Slack/Telegram delivery alongside email
- Add an unsubscribe link in each email
- Move from JSON files to a free hosted DB (e.g. Supabase) for easier scaling
- Add more country-specific official job APIs (e.g. other Nordic countries)
  alongside JobTech, following the same pattern used for Sweden
