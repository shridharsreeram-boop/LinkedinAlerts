# Job Alert Pipeline 🔔

A fully automated, **100% free**, multi-user job alert system. Subscribers sign
up with a target job title, location, and an alert duration. The pipeline
checks for new matching job postings on a schedule, uses an LLM to score how
relevant each posting actually is, and emails a digest of the best matches —
automatically stopping once the subscriber's chosen period ends.

Built to run entirely on free tiers: **GitHub Actions** (compute/scheduling),
**GitHub Pages** (signup page + dashboard), **Adzuna API** (job data),
**Claude API** (relevance scoring), and **Resend** (email delivery).

---

## Why this project

Job boards send a firehose of postings, most irrelevant. This pipeline:
1. Pulls fresh postings for a specific title + location
2. Filters out anything already seen
3. Uses an LLM to actually judge *relevance*, not just keyword matching
4. Emails only what's worth your time
5. Expires automatically — no stale subscriptions

It's also a self-contained demonstration of: API integration, scheduled
automation, AI-assisted filtering, state management (dedup across runs),
and a simple ops dashboard — built without paying for any infrastructure.

---

## Architecture

```
Signup page (GitHub Pages)
        │  (Google Form)
        ▼
Google Sheet  ──sync_signups.py──►  data/subscribers.json
                                            │
                          GitHub Actions cron (every 6h)
                                            │
                                            ▼
                                   scripts/run_pipeline.py
                                            │
                ┌───────────────────────────┼───────────────────────────┐
                ▼                           ▼                           ▼
        Adzuna API (fetch jobs)   Claude API (score relevance)   Resend API (send email)
                │                           │                           │
                └─────────────► data/seen_jobs.json, data/run_log.json ◄┘
                                            │
                                            ▼
                          scripts/generate_dashboard.py → docs/dashboard.html
```

---

## Setup (step by step)

### 1. Create free accounts / API keys
| Service | Purpose | Free tier |
|---|---|---|
| [Adzuna API](https://developer.adzuna.com/) | Job search data | Free, generous limits |
| [Anthropic Console](https://console.anthropic.com/) | Claude API for relevance scoring | Pay-as-you-go, low cost per call (or use the fallback neutral score if you skip this) |
| [Resend](https://resend.com/) | Transactional email | 100 emails/day free |
| GitHub | Hosting, scheduling, Pages | Free for public repos |

### 2. Create the signup form
- Make a Google Form with fields: **Name, Email, Job Title, Location, Country Code, Alert Duration (days)**
- Link it to a Google Sheet (Form automatically does this)
- In the Sheet: **File → Share → Publish to web → CSV**, copy the URL
- Update the form link in `docs/index.html`

### 3. Configure GitHub repo secrets
In **Settings → Secrets and variables → Actions**, add:
- `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`
- `ANTHROPIC_API_KEY`
- `RESEND_API_KEY`
- `ALERT_FROM_EMAIL` (a verified sender on Resend)
- `GOOGLE_SHEET_CSV_URL` (from step 2)

### 4. Enable GitHub Pages
**Settings → Pages → Source: `docs/` folder on `main` branch**

Your signup page will be live at `https://<username>.github.io/<repo>/`
and the dashboard at `.../dashboard.html`.

### 5. Enable the workflow
The pipeline runs automatically every 6 hours via
`.github/workflows/job-alert.yml`. You can also trigger it manually from
the **Actions** tab (`workflow_dispatch`).

---

## Local testing

```bash
pip install requests
export ADZUNA_APP_ID=xxx ADZUNA_APP_KEY=xxx ANTHROPIC_API_KEY=xxx RESEND_API_KEY=xxx ALERT_FROM_EMAIL=alerts@yourdomain.com
python scripts/run_pipeline.py
python scripts/generate_dashboard.py
```

Add a test subscriber manually to `data/subscribers.json`:
```json
[
  {
    "name": "Sriram",
    "email": "you@example.com",
    "job_title": "System Configuration Developer",
    "location": "Gothenburg",
    "country_code": "se",
    "end_date": "2026-12-31"
  }
]
```

---

## Notes on legitimacy

This intentionally does **not** scrape LinkedIn directly — that violates
LinkedIn's Terms of Service. Instead it uses the Adzuna API, a legitimate
job-aggregation service that indexes postings from many sources (including
many that also appear on LinkedIn), which keeps the project both functional
and compliant.

## Possible extensions
- Swap Adzuna for additional aggregators and merge results
- Add Slack/Telegram delivery alongside email
- Add a "snooze" or "unsubscribe" link in each email
- Store data in a free hosted DB (e.g. Supabase) instead of JSON files for easier multi-repo scaling
