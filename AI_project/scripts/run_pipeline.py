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
JSEARCH_API_KEY = os.environ.get("JSEARCH_API_KEY")
ALERT_FROM_EMAIL = os.environ.get("ALERT_FROM_EMAIL", "alerts@yourdomain.com")
PIPELINE_OWNER_EMAIL = os.environ.get("PIPELINE_OWNER_EMAIL", "shridhar.sreeram@gmail.com")

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


def fetch_jobs_india(title, location, results=10):
    """Fetch job postings from JSearch API (via RapidAPI) for Indian locations.
    JSearch aggregates from LinkedIn, Indeed, Glassdoor, Naukri and others
    via Google for Jobs — much better India coverage than Adzuna alone.
    Only called when subscriber's location includes Indian cities.
    Searches each location separately for better coverage."""
    if not JSEARCH_API_KEY:
        print("    [warn] JSEARCH_API_KEY not set, skipping JSearch India fetch")
        return []

    # Split locations and search each one separately for better results
    locations_list = [l.strip() for l in location.split() if l.strip()]
    if not locations_list:
        locations_list = ["India"]

    all_results = []
    for loc in locations_list:
        query = f"{title} {loc}"
        try:
            resp = requests.get(
                "https://jsearch.p.rapidapi.com/search-v2",
                headers={
                    "X-RapidAPI-Key": JSEARCH_API_KEY,
                    "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
                    "Content-Type": "application/json",
                },
                params={
                    "query": query,
                    "page": "1",
                    "num_pages": "1",
                    "date_posted": "month",
                },
                timeout=20,
            )
            if resp.status_code == 403:
                print("    [warn] JSearch API key invalid or not subscribed — check RapidAPI subscription")
                return []
            resp.raise_for_status()
            jobs = resp.json().get("data", {}).get("jobs", [])
            normalized = []
            for j in jobs[:results]:
                normalized.append({
                    "id": f"jsearch-{j.get('job_id', '')}",
                    "title": j.get("job_title", "Untitled role"),
                    "company": {"display_name": j.get("employer_name", "Unknown company")},
                    "location": {"display_name": f"{j.get('job_city', '')} {j.get('job_country', 'India')}".strip()},
                    "description": j.get("job_description", "") or "",
                    "redirect_url": j.get("job_apply_link") or j.get("job_google_link", "#"),
                    "_country": "in",
                    "_source": "JSearch (India)",
                })
            print(f"    [info] JSearch returned {len(normalized)} results for '{query}'")
            all_results.extend(normalized)
            time.sleep(0.3)  # be gentle on rate limits
        except Exception as e:
            print(f"    [warn] JSearch India fetch failed for '{query}': {e}")

    return all_results


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

    prompt = f"""A job seeker is looking for roles matching these keywords: "{subscriber_keywords}"

These keywords may be related — for example "AI" and "CFD" together suggest the person
wants roles at the intersection of AI/ML and computational fluid dynamics, such as
"Simulation Engineer", "Digital Twin Engineer", "Computational Modelling Specialist",
or "ML for Engineering Applications". Score these intersection roles highly even if
neither exact keyword appears in the title.

However, ONLY score a job highly if the role is genuinely relevant to the keywords
as a JOB TITLE — not just because the description mentions the keyword in passing.

Here is a job posting:
Title: {job.get('title')}
Company: {job.get('company', {}).get('display_name', 'Unknown')}
Location: {job.get('location', {}).get('display_name', 'Unknown')}
Description: {job.get('description', '')[:800]}

Score 8-10: Job title directly matches one or more keywords, or is a clear intersection role
Score 5-7: Job title is adjacent/related but not a direct match
Score 1-4: Job only mentions keywords in description, not a real match
Score 0: Completely irrelevant

On a scale of 0 to 10, how relevant is this job to the seeker's interests?
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


def send_welcome_email(to_email, subscriber_name, job_title, location):
    """Send a welcome email to a new subscriber explaining how the service works."""
    if not RESEND_API_KEY:
        return False

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
        <div style="font-size:11px;color:#94a3b8;margin-top:3px;letter-spacing:1px;text-transform:uppercase;">Welcome aboard</div>
      </td>
      <td align="right">
        <span style="background-color:#22c55e;border-radius:20px;padding:5px 14px;font-size:12px;font-weight:600;color:#ffffff;">YOU'RE IN</span>
      </td>
    </tr></table>
  </td></tr>

  <tr><td style="background-color:#0f172a;padding:14px 40px;">
    <p style="margin:0;color:#cbd5e1;font-size:14px;">Hi <strong style="color:#ffffff;">{subscriber_name}</strong> — your job alerts are now active!</p>
  </td></tr>

  <tr><td style="background-color:#ffffff;padding:28px 40px;">
    <p style="margin:0 0 20px 0;color:#1a2332;font-size:15px;">Here's what happens next:</p>

    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      <tr>
        <td width="40" valign="top" style="font-size:20px;">&#128269;</td>
        <td style="padding-left:12px;">
          <p style="margin:0;font-size:14px;font-weight:600;color:#1a2332;">We search every 3 hours</p>
          <p style="margin:4px 0 0 0;font-size:13px;color:#64748b;">We check multiple job sources for <strong>{job_title}</strong> roles in <strong>{location}</strong> — including LinkedIn, Indeed, and local job boards.</p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      <tr>
        <td width="40" valign="top" style="font-size:20px;">&#127919;</td>
        <td style="padding-left:12px;">
          <p style="margin:0;font-size:14px;font-weight:600;color:#1a2332;">Only relevant matches</p>
          <p style="margin:4px 0 0 0;font-size:13px;color:#64748b;">We filter out jobs where your keywords only appear in the description — you only get roles where your title is genuinely what they're hiring for.</p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        <td width="40" valign="top" style="font-size:20px;">&#9993;</td>
        <td style="padding-left:12px;">
          <p style="margin:0;font-size:14px;font-weight:600;color:#1a2332;">Email digest when new jobs appear</p>
          <p style="margin:4px 0 0 0;font-size:13px;color:#64748b;">You'll only get an email when there are genuinely new postings — no email means nothing new was found. Each job card has a direct apply link.</p>
        </td>
      </tr>
    </table>

    <div style="background-color:#f0f9ff;border-radius:8px;border-left:3px solid #0ea5e9;padding:16px 20px;">
      <p style="margin:0;font-size:13px;color:#0369a1;font-weight:600;">Your alert is set up for:</p>
      <p style="margin:6px 0 0 0;font-size:13px;color:#475569;">&#128188; <strong>{job_title}</strong></p>
      <p style="margin:4px 0 0 0;font-size:13px;color:#475569;">&#128205; <strong>{location}</strong></p>
    </div>
  </td></tr>

  <tr><td style="background-color:#1a2332;border-radius:0 0 12px 12px;padding:20px 40px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:12px;color:#64748b;">You signed up at <span style="color:#94a3b8;font-weight:500;">jobpingapp.xyz</span></td>
      <td align="right">
        <a href="https://docs.google.com/forms/d/e/1FAIpQLSdzdAz0mL4Q7NoYWtDWLgICEIIsujieSw7bvy7BEckUjZfF6g/viewform?usp=pp_url&entry.169517527={to_email}"
           style="font-size:12px;color:#0ea5e9;text-decoration:none;font-weight:500;">Unsubscribe</a>
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
            "subject": f"Welcome to jobpingapp — your alerts for {job_title} are live ⚡",
            "html": html,
        },
        timeout=30,
    )
    return resp.status_code < 300


def send_email(to_email, subscriber_name, jobs, job_title="", location=""):
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
            <p style="margin:10px 0 0 0;font-size:12px;">
              <a href="{link}" style="color:#0ea5e9;text-decoration:none;font-weight:500;">View job &rarr;</a>
              &nbsp;&nbsp;&nbsp;
              <a href="https://docs.google.com/forms/d/e/1FAIpQLSdkCwZB3_-gS7Pq9TOaAEEZomELxWxuRv5JiNN4-qTAh0JjWw/viewform?usp=pp_url&entry.1798283264={to_email}&entry.158842040={title}&entry.2135608452={company}"
                 style="color:#94a3b8;text-decoration:none;font-size:11px;">&#10003; Mark as applied</a>
            </p>
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
      <td style="font-size:12px;color:#64748b;">You signed up for alerts at <span style="color:#94a3b8;font-weight:500;">jobpingapp.xyz</span></td>
      <td align="right" style="font-size:12px;">
        <span style="color:#475569;">Helpful?</span>
        <a href="https://docs.google.com/forms/d/e/1FAIpQLSdkCwZB3_-gS7Pq9TOaAEEZomELxWxuRv5JiNN4-qTAh0JjWw/viewform?usp=pp_url&entry.1798283264={to_email}&entry.158842040=👍+Yes&entry.2135608452=feedback"
           style="color:#22c55e;text-decoration:none;margin:0 6px;">👍</a>
        <a href="https://docs.google.com/forms/d/e/1FAIpQLSdkCwZB3_-gS7Pq9TOaAEEZomELxWxuRv5JiNN4-qTAh0JjWw/viewform?usp=pp_url&entry.1798283264={to_email}&entry.158842040=👎+No&entry.2135608452=feedback"
           style="color:#ef4444;text-decoration:none;margin-right:12px;">👎</a>
        <a href="https://docs.google.com/forms/d/e/1FAIpQLSdzdAz0mL4Q7NoYWtDWLgICEIIsujieSw7bvy7BEckUjZfF6g/viewform?usp=pp_url&entry.169517527={to_email}"
           style="font-size:12px;color:#0ea5e9;text-decoration:none;font-weight:500;">Unsubscribe</a>
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
            "subject": f"{len(jobs)} new match(es) for {job_title or 'your search'}{' in ' + location if location else ''}",
            "html": html,
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"  [error] Resend API failed: {resp.status_code} {resp.text}")
        return False
    return True


# ---------- Keyword expansions ----------
KEYWORD_EXPANSIONS = {
    # Engineering / CFD
    "cfd": ["computational fluid dynamics", "fluid simulation", "ansys", "openfoam",
            "fluent", "star-ccm", "cfx", "flow simulation", "aerodynamics",
            "computational fluid", "fluid mechanics"],
    "fem": ["finite element", "finite element analysis", "fea", "abaqus",
            "nastran", "ansys mechanical", "structural simulation"],
    "fea": ["finite element analysis", "finite element", "fem", "abaqus",
            "nastran", "structural analysis", "structural simulation"],
    "ansys": ["cfd", "finite element", "simulation engineer", "fea", "fluent",
              "mechanical simulation"],
    "thermal": ["heat transfer", "thermodynamics", "thermal analysis",
                "thermal design", "thermal management", "hvac", "cooling"],
    "hvac": ["heating ventilation", "air conditioning", "thermal systems",
             "building services", "mechanical services", "mep"],

    # AI / ML
    "ai": ["artificial intelligence", "machine learning", "deep learning",
           "neural network", "llm", "large language model", "generative ai",
           "gen ai", "computer vision", "natural language processing"],
    "ml": ["machine learning", "deep learning", "artificial intelligence",
           "neural network", "data science", "predictive modelling",
           "mlops", "model training"],
    "nlp": ["natural language processing", "text mining", "computational linguistics",
            "language model", "speech recognition", "text analytics"],
    "cv": ["computer vision", "image processing", "object detection",
           "image recognition", "deep learning vision", "opencv"],
    "llm": ["large language model", "generative ai", "gpt", "llama",
            "prompt engineering", "fine tuning", "rag"],
    "data science": ["machine learning", "data analytics", "statistical modelling",
                     "python analytics", "data mining", "business intelligence"],
    "data engineer": ["data pipeline", "etl", "spark", "kafka", "airflow",
                      "databricks", "snowflake", "data warehouse"],

    # Software / Web
    "python": ["django", "flask", "fastapi", "data engineering", "python developer",
               "backend python", "python engineer"],
    "java": ["spring boot", "microservices java", "j2ee", "backend java",
             "java developer", "java engineer"],
    "javascript": ["react", "node.js", "typescript", "frontend", "vue", "angular",
                   "fullstack javascript"],
    "react": ["frontend developer", "react developer", "reactjs", "next.js",
              "frontend engineer", "ui developer"],
    "devops": ["site reliability", "sre", "platform engineering", "cloud infrastructure",
               "ci/cd", "kubernetes", "docker", "devsecops"],
    "fullstack": ["full stack", "full-stack", "frontend backend", "web development",
                  "mern", "mean stack"],
    "backend": ["api developer", "server side", "microservices", "rest api",
                "backend engineer", "backend developer"],
    "cloud": ["aws", "azure", "gcp", "google cloud", "cloud architect",
              "cloud engineer", "devops cloud"],
    "aws": ["amazon web services", "cloud engineer", "solutions architect",
            "cloud infrastructure", "serverless"],

    # Mobile
    "ios": ["swift", "objective-c", "xcode", "mobile development apple",
            "ios developer", "ios engineer"],
    "android": ["kotlin", "java mobile", "mobile development android",
                "android developer", "android engineer"],
    "flutter": ["dart", "cross platform mobile", "flutter developer",
                "mobile developer"],

    # Mechanical / Manufacturing
    "mechanical engineer": ["mechanical design", "product design", "cad engineer",
                            "design engineer", "r&d engineer"],
    "solidworks": ["cad", "mechanical design", "product design", "3d modelling",
                   "catia", "nx cad"],
    "embedded": ["firmware", "rtos", "microcontroller", "embedded c",
                 "embedded systems", "iot firmware", "arm cortex"],
    "iot": ["internet of things", "embedded systems", "firmware", "connected devices",
            "edge computing"],

    # Telecom
    "telecom": ["telecommunications", "5g", "lte", "network engineer",
                "rf engineer", "wireless engineer"],
    "5g": ["telecommunications", "lte", "nr", "radio access network",
           "ran engineer", "wireless"],

    # Management / Scrum
    "scrum master": ["agile coach", "agile master", "sprint planning",
                     "scrum", "agile delivery"],
    "project manager": ["programme manager", "delivery manager", "project lead",
                        "project coordinator", "pmo"],
    "product manager": ["product owner", "product lead", "product development",
                        "product strategy"],

    # Data / Analytics
    "sql": ["database developer", "data analyst", "business intelligence",
            "mysql", "postgresql", "data warehouse"],
    "power bi": ["business intelligence", "data visualisation", "tableau",
                 "reporting analyst", "bi developer"],
}


def get_expanded_keywords(titles):
    """Return all search terms for a list of keywords — original terms
    plus any expansions from the dictionary."""
    expanded = set()
    for title in titles:
        title_lower = title.lower().strip()
        expanded.add(title_lower)
        if title_lower in KEYWORD_EXPANSIONS:
            expanded.update(KEYWORD_EXPANSIONS[title_lower])
    return expanded


# ---------- Main ----------
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    subscribers = load_json(SUBSCRIBERS_FILE, [])
    seen_jobs = load_json(SEEN_JOBS_FILE, {})  # {subscriber_email: [job_ids]}
    run_log = load_json(LOG_FILE, [])

    active_subscribers = []
    run_summary = {"timestamp": datetime.datetime.utcnow().isoformat(), "results": []}

    new_subscriber_emails = set()
    if os.environ.get("NEW_SUBSCRIBERS", "0") != "0":
        # Collect emails of subscribers who just joined this run
        for sub in active_subscribers:
            if sub.get("signup_timestamp") and sub.get("signed_up") == datetime.date.today().isoformat():
                new_subscriber_emails.add(sub.get("email"))

    for sub in subscribers:
        email = sub.get("email")
        if is_expired(sub):
            print(f"Skipping expired subscriber: {email}")
            continue
        active_subscribers.append(sub)

        title_raw = sub.get("job_title", "")
        location_raw = sub.get("location", "")

        # Send expiry warning 3 days before subscription ends
        end_date = sub.get("end_date")
        if end_date:
            days_left = (datetime.date.fromisoformat(end_date) - datetime.date.today()).days
            if days_left == 3:
                _send_expiry_warning(email, sub.get("name", "there"), title_raw, location_raw, end_date)
                print(f"  [info] Expiry warning sent to {email} (expires {end_date})")

        # Send welcome email if this is a new subscriber
        if email in new_subscriber_emails:
            send_welcome_email(email, sub.get("name", "there"), title_raw, location_raw)
            print(f"  [info] Welcome email sent to {email}")

        # Support comma-separated job titles and locations
        # Each title is searched with all locations combined as context
        titles = [t.strip() for t in title_raw.split(",") if t.strip()]
        locations = [l.strip() for l in location_raw.split(",") if l.strip()]
        if not titles:
            titles = [""]
        combined_location = " ".join(locations)  # combine all locations into one search string

        print(f"Checking jobs for {email}: titles={titles} locations={locations}")

        # Only call Sweden's JobTech API if the subscriber's locations
        # include a Swedish city — avoids surfacing Swedish jobs for
        # users searching in India, UK, Germany etc.
        SWEDISH_KEYWORDS = {
            "sweden", "sverige", "se",
            "stockholm", "gothenburg", "göteborg", "goteborg",
            "malmö", "malmo", "malmoe",
            "uppsala", "karlstad", "linköping", "linkoping",
            "lund", "örebro", "orebro", "västerås", "vasteras",
            "helsingborg", "norrköping", "norrkoping", "jönköping",
            "jonkoping", "umeå", "umea", "gävle", "gavle",
        }
        INDIAN_KEYWORDS = {
            "india", "bangalore", "bengaluru", "mumbai", "delhi", "new delhi",
            "hyderabad", "chennai", "pune", "kolkata", "ahmedabad", "noida",
            "gurugram", "gurgaon", "chandigarh", "jaipur", "kochi", "indore",
            "coimbatore", "nagpur", "vizag", "visakhapatnam", "surat",
        }
        locations_lower = {l.lower() for l in locations}
        include_sweden = bool(locations_lower & SWEDISH_KEYWORDS)
        include_india = bool(locations_lower & INDIAN_KEYWORDS)

        try:
            jobs = []
            for title in titles:
                if include_sweden:
                    jobs += fetch_jobs_sweden(title, combined_location)
                if include_india:
                    jobs += fetch_jobs_india(title, combined_location)
                jobs += fetch_jobs(title, combined_location)
        except Exception as e:
            print(f"  [error] fetch failed for {email}: {e}")
            run_summary["results"].append({"email": email, "status": "fetch_error", "detail": str(e)})
            continue

        # Use all titles as keywords for relevance scoring
        scoring_keywords = title_raw

        jobs = merge_cross_source_duplicates(jobs)
        merged_count = sum(1 for j in jobs if j.get("_sources"))
        if merged_count:
            print(f"  [info] merged {merged_count} job(s) found on multiple sources")

        # Filter out stale listings older than 30 days
        # Adzuna sometimes returns old listings that are no longer active
        cutoff_date = datetime.date.today() - datetime.timedelta(days=30)
        fresh_jobs = []
        stale_count = 0
        for job in jobs:
            created = job.get("created")
            if created:
                try:
                    job_date = datetime.date.fromisoformat(created[:10])
                    if job_date < cutoff_date:
                        stale_count += 1
                        continue
                except Exception:
                    pass
            fresh_jobs.append(job)
        if stale_count > 0:
            print(f"  [info] filtered out {stale_count} stale listing(s) older than 30 days")
        jobs = fresh_jobs

        # Title-strict filtering with keyword expansions:
        # Match job titles against both the original keywords AND their
        # expanded forms (e.g. CFD → "Computational Fluid Dynamics", "ANSYS")
        expanded_keywords = get_expanded_keywords(titles)

        def title_matches(job):
            job_title_lower = (job.get("title") or "").lower()
            return any(kw in job_title_lower for kw in expanded_keywords)

        title_matched = [j for j in jobs if title_matches(j)]
        title_filtered_out = len(jobs) - len(title_matched)
        if title_filtered_out > 0:
            print(f"  [info] filtered out {title_filtered_out} job(s) where keyword only appeared in description")

        # Cross-keyword dedup: if the same job matched multiple keyword
        # searches (e.g. appears in both "CFD" and "Fluid dynamics" results),
        # only include it once in the final email
        seen_ids_this_run = set()
        deduped_jobs = []
        for job in title_matched:
            job_id = str(job.get("id"))
            if job_id not in seen_ids_this_run:
                seen_ids_this_run.add(job_id)
                deduped_jobs.append(job)
        cross_dupes = len(title_matched) - len(deduped_jobs)
        if cross_dupes > 0:
            print(f"  [info] removed {cross_dupes} duplicate(s) matched by multiple keywords")
        jobs = deduped_jobs

        already_seen = set(seen_jobs.get(email, []))
        new_jobs = [j for j in jobs if str(j.get("id")) not in already_seen]

        relevant_jobs = []
        for job in new_jobs:
            score = score_relevance(job, scoring_keywords)
            if score >= RELEVANCE_THRESHOLD:
                relevant_jobs.append((job, score))
            time.sleep(0.5)  # be gentle on API rate limits

        # Sort by relevance score descending and cap at 20
        relevant_jobs = sorted(relevant_jobs, key=lambda x: x[1], reverse=True)[:20]

        # mark all new jobs (relevant or not) as seen so we don't re-score them
        seen_jobs.setdefault(email, [])
        seen_jobs[email].extend(str(j.get("id")) for j in new_jobs)

        if relevant_jobs:
            sent = send_email(email, sub.get("name", "there"), relevant_jobs, title_raw, location_raw)
            status = "sent" if sent else "email_failed"
        elif email in new_subscriber_emails:
            # New subscriber but no jobs found yet — send a friendly holding email
            _send_no_jobs_yet_email(email, sub.get("name", "there"), title_raw, location_raw)
            status = "no_jobs_yet_email_sent"
        else:
            status = "no_new_relevant_jobs"

        # Count jobs by source for dashboard reporting
        source_counts = {}
        for job, score in relevant_jobs:
            source = job.get("_source", "Unknown")
            source_counts[source] = source_counts.get(source, 0) + 1

        print(f"  -> {status} ({len(relevant_jobs)} relevant of {len(new_jobs)} new)")
        run_summary["results"].append({
            "email": email,
            "status": status,
            "new_jobs_found": len(new_jobs),
            "relevant_jobs_sent": len(relevant_jobs),
            "source_counts": source_counts,
        })

    # Persist state
    save_json(SUBSCRIBERS_FILE, active_subscribers)  # drops expired subscribers
    save_json(SEEN_JOBS_FILE, seen_jobs)
    run_log.append(run_summary)
    run_log = run_log[-100:]  # keep last 100 runs
    save_json(LOG_FILE, run_log)

    print("Pipeline run complete.")

    # Send error alert to pipeline owner if anything went wrong
    error_results = [r for r in run_summary["results"]
                     if r.get("status") in ("fetch_error", "email_failed")]
    if error_results:
        _send_error_alert(error_results)


def _send_no_jobs_yet_email(to_email, name, job_title, location):
    """Send a friendly email to new subscribers when no matching jobs are found yet."""
    if not RESEND_API_KEY:
        return
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": ALERT_FROM_EMAIL,
                "to": [to_email],
                "subject": f"Your jobpingapp alerts are set up — watching for {job_title} roles ⚡",
                "html": f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <tr><td style="background-color:#1a2332;border-radius:12px 12px 0 0;padding:28px 40px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td>
        <div style="font-size:20px;font-weight:700;color:#ffffff;">&#9889; jobpingapp</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:3px;letter-spacing:1px;text-transform:uppercase;">Watching for your roles</div>
      </td>
      <td align="right">
        <span style="background-color:#f59e0b;border-radius:20px;padding:5px 14px;font-size:12px;font-weight:600;color:#ffffff;">SCANNING</span>
      </td>
    </tr></table>
  </td></tr>

  <tr><td style="background-color:#0f172a;padding:14px 40px;">
    <p style="margin:0;color:#cbd5e1;font-size:14px;">Hi <strong style="color:#ffffff;">{name}</strong> — we're on it.</p>
  </td></tr>

  <tr><td style="background-color:#ffffff;padding:32px 40px;">
    <p style="margin:0 0 20px 0;font-size:15px;color:#1a2332;">We just searched for <strong>{job_title}</strong> roles in <strong>{location}</strong> and didn't find any new matches right now.</p>
    <p style="margin:0 0 28px 0;font-size:14px;color:#64748b;">That's completely normal — job boards update constantly. We'll check again in 3 hours and email you the moment something relevant appears.</p>

    <div style="background-color:#f8fafc;border-radius:12px;padding:24px 28px;border-left:4px solid #0ea5e9;margin-bottom:24px;">
      <p style="margin:0 0 8px 0;font-size:22px;color:#1a2332;">&#8220;</p>
      <p style="margin:0 0 12px 0;font-size:15px;color:#334155;font-style:italic;line-height:1.6;">The secret of getting ahead is getting started. The right opportunity is already out there — we'll find it for you.</p>
      <p style="margin:0;font-size:13px;color:#94a3b8;">— Mark Twain</p>
    </div>

    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="background-color:#f0f9ff;border-radius:8px;padding:16px 20px;">
          <p style="margin:0;font-size:13px;color:#0369a1;font-weight:600;">Your alert is active for:</p>
          <p style="margin:6px 0 0 0;font-size:13px;color:#475569;">&#128188; <strong>{job_title}</strong></p>
          <p style="margin:4px 0 0 0;font-size:13px;color:#475569;">&#128205; <strong>{location}</strong></p>
          <p style="margin:8px 0 0 0;font-size:12px;color:#64748b;">Checking every 3 hours across LinkedIn, Indeed, local job boards and more.</p>
        </td>
      </tr>
    </table>
  </td></tr>

  <tr><td style="background-color:#1a2332;border-radius:0 0 12px 12px;padding:20px 40px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:12px;color:#64748b;">You signed up at <span style="color:#94a3b8;font-weight:500;">jobpingapp.xyz</span></td>
      <td align="right">
        <a href="https://docs.google.com/forms/d/e/1FAIpQLSdzdAz0mL4Q7NoYWtDWLgICEIIsujieSw7bvy7BEckUjZfF6g/viewform?usp=pp_url&entry.169517527={to_email}"
           style="font-size:12px;color:#0ea5e9;text-decoration:none;font-weight:500;">Unsubscribe</a>
      </td>
    </tr></table>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>""",
            },
            timeout=30,
        )
    except Exception as e:
        print(f"  [warn] No-jobs-yet email send failed: {e}")


def _send_expiry_warning(to_email, name, job_title, location, end_date):
    """Send a warning email 3 days before a subscription expires."""
    if not RESEND_API_KEY:
        return
    signup_url = "https://jobpingapp.xyz"
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": ALERT_FROM_EMAIL,
                "to": [to_email],
                "subject": f"⏰ Your jobpingapp alerts expire in 3 days",
                "html": f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;">
  <tr><td style="background-color:#1a2332;padding:24px 40px;">
    <div style="font-size:20px;font-weight:700;color:#ffffff;">&#9889; jobpingapp</div>
  </td></tr>
  <tr><td style="padding:32px 40px;">
    <p style="margin:0 0 16px 0;font-size:16px;color:#1a2332;">Hi <strong>{name}</strong>,</p>
    <p style="margin:0 0 16px 0;font-size:14px;color:#475569;">Your job alerts for <strong>{job_title}</strong> in <strong>{location}</strong> will expire on <strong>{end_date}</strong> — that's in 3 days.</p>
    <p style="margin:0 0 24px 0;font-size:14px;color:#475569;">If you'd like to keep receiving alerts, sign up again below:</p>
    <a href="{signup_url}" style="display:inline-block;background-color:#0ea5e9;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:6px;font-size:14px;font-weight:600;">Renew my alerts →</a>
  </td></tr>
  <tr><td style="background-color:#f8fafc;padding:16px 40px;border-top:1px solid #e2e8f0;">
    <p style="margin:0;font-size:12px;color:#94a3b8;">jobpingapp.xyz — automated job alerts</p>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>""",
            },
            timeout=30,
        )
    except Exception as e:
        print(f"  [warn] Expiry warning send failed: {e}")


def _send_error_alert(errors):
    """Email the pipeline owner when something goes wrong in a run."""
    if not RESEND_API_KEY or not PIPELINE_OWNER_EMAIL:
        print("  [warn] Cannot send error alert — RESEND_API_KEY or PIPELINE_OWNER_EMAIL not set")
        return
    error_html = "".join(
        f"<li><strong>{e.get('email', '?')}</strong>: {e.get('status')} — {e.get('detail', 'no detail')}</li>"
        for e in errors
    )
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": ALERT_FROM_EMAIL,
                "to": [PIPELINE_OWNER_EMAIL],
                "subject": f"⚠️ Job Alert Pipeline — {len(errors)} error(s) detected",
                "html": f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;">
  <tr><td style="background-color:#dc2626;padding:24px 40px;">
    <div style="font-size:20px;font-weight:700;color:#ffffff;">⚠️ Pipeline Error Alert</div>
    <div style="font-size:13px;color:#fca5a5;margin-top:4px;">jobpingapp.xyz — action required</div>
  </td></tr>
  <tr><td style="padding:28px 40px;">
    <p style="margin:0 0 16px 0;color:#1a2332;font-size:15px;">{len(errors)} issue(s) detected in the latest pipeline run:</p>
    <ul style="margin:0 0 24px 0;padding-left:20px;color:#475569;font-size:14px;line-height:1.8;">
      {error_html}
    </ul>
    <a href="https://github.com/shridharsreeram-boop/LinkedinAlerts/actions"
       style="display:inline-block;background-color:#1a2332;color:#ffffff;text-decoration:none;padding:10px 20px;border-radius:6px;font-size:13px;font-weight:600;">
      View Actions Logs →
    </a>
  </td></tr>
  <tr><td style="background-color:#f8fafc;padding:16px 40px;border-top:1px solid #e2e8f0;">
    <p style="margin:0;font-size:12px;color:#94a3b8;">This is an automated alert from your job alert pipeline.</p>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>""",
            },
            timeout=30,
        )
        if resp.status_code < 300:
            print(f"  [info] Error alert sent to {PIPELINE_OWNER_EMAIL}")
        else:
            print(f"  [warn] Failed to send error alert: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  [warn] Error alert send failed: {e}")


if __name__ == "__main__":
    main()
