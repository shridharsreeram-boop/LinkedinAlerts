#!/usr/bin/env python3
"""
Job Alert Pipeline
------------------
1. Reads subscribers from data/subscribers.json
2. Removes expired subscriptions
3. For each active subscriber, fetches new job postings matching their
   title + location from TWO sources:
     - Sweden's free, official JobTech/Platsbanken API (no key needed)
     - Adzuna API, looped across ~19 supported countries (Adzuna has no
       Sweden coverage, so this catches everything else)
4. Detects when the SAME job posting (same title + same company) appears
   in both sources, and merges them into a single entry listing both
   source links, instead of sending a duplicate.
5. Filters out jobs already seen (data/seen_jobs.json)
6. Uses Claude API to score relevance of each new job to the subscriber's
   stated title/keywords
7. Sends an email digest of high-relevance new jobs via Resend
8. Updates seen_jobs.json and writes a log entry for the dashboard

All credentials are read from environment variables (set as GitHub Actions
secrets - never hardcoded).
"""

import os
import re
import json
import time
import datetime
import requests

# ---------- Config ----------
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ALERT_FROM_EMAIL = os.environ.get("ALERT_FROM_EMAIL", "alerts@yourdomain.com")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")
SEEN_JOBS_FILE = os.path.join(DATA_DIR, "seen_jobs.json")
LOG_FILE = os.path.join(DATA_DIR, "run_log.json")

ADZUNA_COUNTRIES = ["gb", "us", "au", "at", "be", "br", "ca", "ch", "de", "es",
                     "fr", "in", "it", "mx", "nl", "nz", "pl", "sg", "za"]
RELEVANCE_THRESHOLD = 6  # out of 10, minimum score to include a job in the email


# ---------- Helpers ----------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def is_expired(subscriber):
    end_date = subscriber.get("end_date")
    if not end_date:
        return False
    return datetime.date.today() > datetime.date.fromisoformat(end_date)


def fetch_jobs_sweden(title, location, results=20):
    """Fetch job postings from Sweden's free, official JobTech/Platsbanken API.
    No API key required. Uses free-text search combining title + location.
    Results are normalized to match Adzuna's job dict shape so the rest of
    the pipeline (scoring, email, dedup) works unchanged."""
    query = f"{title} {location}".strip()
    try:
        resp = requests.get(
            "https://jobsearch.api.jobtechdev.se/search",
            params={"q": query, "limit": results},
            headers={"accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        normalized = []
        for h in hits:
            employer = h.get("employer") or {}
            workplace = h.get("workplace_address") or {}
            description = h.get("description") or {}
            application = h.get("application_details") or {}
            normalized.append({
                "id": f"se-{h.get('id')}",
                "title": h.get("headline", "Untitled role"),
                "company": {"display_name": employer.get("name", "Unknown company")},
                "location": {"display_name": workplace.get("municipality", "Sweden")},
                "description": description.get("text", "") or "",
                "redirect_url": application.get("url") or h.get("webpage_url", "#"),
                "_country": "se",
                "_source": "JobTech (Sweden)",
            })
        return normalized
    except Exception as e:
        print(f"    [warn] Sweden (JobTech) fetch failed: {e}")
        return []


def fetch_jobs(title, location, countries=None, results_per_country=10):
    """Fetch recent job postings from the Adzuna API across multiple countries."""
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        raise RuntimeError("Missing ADZUNA_APP_ID / ADZUNA_APP_KEY environment variables")

    countries = countries or ADZUNA_COUNTRIES
    all_results = []
    for country in countries:
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
        params = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "what": title,
            "where": location,
            "results_per_page": results_per_country,
            "sort_by": "date",
            "content-type": "application/json",
        }
        try:
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                for job in resp.json().get("results", []):
                    job["_country"] = country
                    job["_source"] = f"Adzuna ({country.upper()})"
                    all_results.append(job)
            # silently skip countries that error (e.g. rate limits) - others still proceed
        except Exception as e:
            print(f"    [warn] fetch failed for country '{country}': {e}")
        time.sleep(0.3)  # be gentle on rate limits across many sequential calls
    return all_results


def _normalize_for_match(text):
    """Lowercase, strip punctuation/extra whitespace for fuzzy comparison."""
    text = (text or "").lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def merge_cross_source_duplicates(jobs):
    """Detect the same job posting appearing in multiple sources (e.g. both
    JobTech and Adzuna often surface listings that originated on LinkedIn or
    elsewhere). Jobs are considered the same if they have a matching
    normalized title AND matching normalized company name. Matches are
    merged into a single entry with a combined list of source links rather
    than being sent as duplicates."""
    merged = []
    seen_keys = {}

    for job in jobs:
        title_key = _normalize_for_match(job.get("title"))
        company_key = _normalize_for_match(
            (job.get("company") or {}).get("display_name")
        )
        match_key = (title_key, company_key)

        if title_key and company_key and match_key in seen_keys:
            existing = seen_keys[match_key]
            existing.setdefault("_sources", [])
            if not existing["_sources"]:
                # first time we're merging - seed with the original entry's own source
                existing["_sources"].append({
                    "name": existing.get("_source", "Unknown source"),
                    "url": existing.get("redirect_url", "#"),
                })
            existing["_sources"].append({
                "name": job.get("_source", "Unknown source"),
                "url": job.get("redirect_url", "#"),
            })
            # keep the existing entry as the canonical one; skip adding a duplicate
            continue

        merged.append(job)
        if title_key and company_key:
            seen_keys[match_key] = job

    return merged


def score_relevance(job, subscriber_keywords):
    """Ask Claude how relevant a job posting is to the subscriber's stated interests.
    Returns an integer 0-10. Falls back to a neutral score on any API error."""
    if not ANTHROPIC_API_KEY:
        return 7  # neutral fallback if no API key configured

    prompt = f"""A job seeker is interested in roles matching: "{subscriber_keywords}"

Here is a job posting:
Title: {job.get('title')}
Company: {job.get('company', {}).get('display_name', 'Unknown')}
Location: {job.get('location', {}).get('display_name', 'Unknown')}
Description: {job.get('description', '')[:800]}

On a scale of 0 to 10, how relevant is this job posting to the job seeker's interests?
Respond with ONLY the number, nothing else."""

    resp = None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        return int("".join(ch for ch in text if ch.isdigit())[:2] or "7")
    except Exception as e:
        print(f"  [warn] relevance scoring failed: {e}")
        if resp is not None:
            print(f"  [warn] response body: {resp.text}")
        return 7


def send_email(to_email, subscriber_name, jobs):
    """Send a digest email via Resend."""
    if not RESEND_API_KEY:
        print("  [warn] RESEND_API_KEY not set, skipping email send")
        return False

    job_html = ""
    for job, score in jobs:
        title = job.get("title", "Untitled role")
        company = job.get("company", {}).get("display_name", "Unknown company")
        location = job.get("location", {}).get("display_name", "Unknown location")
        link = job.get("redirect_url", "#")
        sources = job.get("_sources")

        if sources:
            source_links = " &nbsp;&middot;&nbsp; ".join(
                f'<a href="{s["url"]}" style="color:#d97706;text-decoration:none;">{s["name"]}</a>'
                for s in sources
            )
            source_line = f'''
            <p style="margin:8px 0 0 0;font-size:12px;color:#92400e;">
              &#9889; Also found on: {source_links}
            </p>'''
            card_style = "margin-bottom:16px;border:1px solid #fde68a;border-radius:8px;border-top:3px solid #f59e0b;overflow:hidden;"
            cell_style = "padding:16px 20px;background-color:#fffbf0;"
        else:
            single_source = job.get("_source", "")
            source_line = f'<p style="margin:8px 0 0 0;font-size:12px;color:#94a3b8;">Source: {single_source}</p>' if single_source else ""
            card_style = "margin-bottom:16px;border:1px solid #e2e8f0;border-radius:8px;border-top:3px solid #0ea5e9;overflow:hidden;"
            cell_style = "padding:16px 20px;background-color:#f8fafc;"

        job_html += f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="{card_style}">
          <tr><td style="{cell_style}">
            <a href="{link}" style="font-size:16px;font-weight:600;color:#1a2332;text-decoration:none;display:block;margin-bottom:6px;">{title}</a>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-size:13px;color:#475569;">{company} &nbsp;&middot;&nbsp; {location}</td>
                <td align="right" style="font-size:12px;color:#94a3b8;white-space:nowrap;">Score: <strong style="color:#0ea5e9;">{score}/10</strong></td>
              </tr>
            </table>
            {source_line}
          </td></tr>
        </table>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <tr><td style="background-color:#1a2332;border-radius:12px 12px 0 0;padding:28px 40px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td>
        <div style="font-size:20px;font-weight:700;color:#ffffff;letter-spacing:-0.5px;">&#9889; jobpingapp</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:3px;letter-spacing:1px;text-transform:uppercase;">Job Alert</div>
      </td>
      <td align="right">
        <span style="background-color:#0ea5e9;border-radius:20px;padding:5px 14px;font-size:12px;font-weight:600;color:#ffffff;letter-spacing:0.5px;">NEW MATCHES</span>
      </td>
    </tr></table>
  </td></tr>

  <tr><td style="background-color:#0f172a;padding:14px 40px;">
    <p style="margin:0;color:#cbd5e1;font-size:14px;">Hi <strong style="color:#ffffff;">{subscriber_name}</strong> — here are your latest matches</p>
  </td></tr>

  <tr><td style="background-color:#ffffff;padding:28px 40px;">
    <p style="margin:0 0 20px 0;font-size:12px;color:#64748b;font-weight:500;text-transform:uppercase;letter-spacing:1px;">{len(jobs)} new posting(s) found</p>
    {job_html}
  </td></tr>

  <tr><td style="background-color:#1a2332;border-radius:0 0 12px 12px;padding:20px 40px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:12px;color:#475569;">Signed up at <span style="color:#94a3b8;">jobpingapp.xyz</span></td>
      <td align="right">
        <a href="https://docs.google.com/forms/d/e/1FAIpQLSdzdAz0mL4Q7NoYWtDWLgICEIIsujieSw7bvy7BEckUjZfF6g/viewform?usp=pp_url&entry.169517527={to_email}"
           style="font-size:12px;color:#64748b;text-decoration:none;border-bottom:1px solid #334155;padding-bottom:1px;">Unsubscribe</a>
      </td>
    </tr></table>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": ALERT_FROM_EMAIL,
            "to": [to_email],
            "subject": f"{len(jobs)} new job match(es) for you",
            "html": html,
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"  [error] Resend API failed: {resp.status_code} {resp.text}")
        return False
    return True


# ---------- Main ----------
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    subscribers = load_json(SUBSCRIBERS_FILE, [])
    seen_jobs = load_json(SEEN_JOBS_FILE, {})  # {subscriber_email: [job_ids]}
    run_log = load_json(LOG_FILE, [])

    active_subscribers = []
    run_summary = {"timestamp": datetime.datetime.utcnow().isoformat(), "results": []}

    for sub in subscribers:
        email = sub.get("email")
        if is_expired(sub):
            print(f"Skipping expired subscriber: {email}")
            continue
        active_subscribers.append(sub)

        title = sub.get("job_title", "")
        location = sub.get("location", "")
        print(f"Checking jobs for {email}: '{title}' in '{location}' (Sweden + all Adzuna countries)")

        try:
            jobs = fetch_jobs_sweden(title, location)
            jobs += fetch_jobs(title, location)
        except Exception as e:
            print(f"  [error] fetch failed for {email}: {e}")
            run_summary["results"].append({"email": email, "status": "fetch_error", "detail": str(e)})
            continue

        jobs = merge_cross_source_duplicates(jobs)
        merged_count = sum(1 for j in jobs if j.get("_sources"))
        if merged_count:
            print(f"  [info] merged {merged_count} job(s) found on multiple sources")

        already_seen = set(seen_jobs.get(email, []))
        new_jobs = [j for j in jobs if str(j.get("id")) not in already_seen]

        relevant_jobs = []
        for job in new_jobs:
            score = score_relevance(job, title)
            if score >= RELEVANCE_THRESHOLD:
                relevant_jobs.append((job, score))
            time.sleep(0.5)  # be gentle on API rate limits

        # mark all new jobs (relevant or not) as seen so we don't re-score them
        seen_jobs.setdefault(email, [])
        seen_jobs[email].extend(str(j.get("id")) for j in new_jobs)

        if relevant_jobs:
            sent = send_email(email, sub.get("name", "there"), relevant_jobs)
            status = "sent" if sent else "email_failed"
        else:
            status = "no_new_relevant_jobs"

        print(f"  -> {status} ({len(relevant_jobs)} relevant of {len(new_jobs)} new)")
        run_summary["results"].append({
            "email": email,
            "status": status,
            "new_jobs_found": len(new_jobs),
            "relevant_jobs_sent": len(relevant_jobs),
        })

    # Persist state
    save_json(SUBSCRIBERS_FILE, active_subscribers)  # drops expired subscribers
    save_json(SEEN_JOBS_FILE, seen_jobs)
    run_log.append(run_summary)
    run_log = run_log[-100:]  # keep last 100 runs
    save_json(LOG_FILE, run_log)

    print("Pipeline run complete.")


if __name__ == "__main__":
    main()
