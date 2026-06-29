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
4. Filters out jobs already seen (data/seen_jobs.json)
5. Uses Claude API to score relevance of each new job to the subscriber's
   stated title/keywords
6. Sends an email digest of high-relevance new jobs via Resend
7. Updates seen_jobs.json and writes a log entry for the dashboard

All credentials are read from environment variables (set as GitHub Actions
secrets - never hardcoded).
"""

import os
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
            })
        return normalized
    except Exception as e:
        print(f"  [warn] relevance scoring failed: {e}")
        try:
            print(f"  [warn] response body: {resp.text}")
        except Exception:
            pass
        return 7


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
                    all_results.append(job)
            # silently skip countries that error (e.g. rate limits) - others still proceed
        except Exception as e:
            print(f"    [warn] fetch failed for country '{country}': {e}")
        time.sleep(0.3)  # be gentle on rate limits across many sequential calls
    return all_results


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
        job_html += f"""
        <div style="margin-bottom:16px;padding:12px;border:1px solid #e0e0e0;border-radius:8px;">
          <a href="{link}" style="font-size:16px;font-weight:bold;color:#0a66c2;text-decoration:none;">{title}</a>
          <p style="margin:4px 0;color:#444;">{company} &middot; {location}</p>
          <p style="margin:0;color:#888;font-size:13px;">Relevance score: {score}/10</p>
        </div>"""

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
      <h2>New job matches for you, {subscriber_name}</h2>
      <p>{len(jobs)} new relevant posting(s) found:</p>
      {job_html}
      <p style="color:#999;font-size:12px;margin-top:24px;">
        You're receiving this because you signed up for job alerts.
      </p>
    </div>"""

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
