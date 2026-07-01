#!/usr/bin/env python3
"""
sync_signups.py
----------------
Pulls new signups from a Google Sheet (used as a free form backend for the
signup page in docs/index.html) and merges them into data/subscribers.json.
Also pulls unsubscribe requests from a second Google Sheet and removes
matching subscribers.

Setup (signup form):
1. Google Form with fields: Name, Email, Job Title, Location, Country
   Code, Alert Duration (days).
2. Link it to a Google Sheet, publish that Sheet as CSV, put the URL in
   the GOOGLE_SHEET_CSV_URL env var / secret.

Setup (unsubscribe form):
1. A second, simple Google Form with just an Email field.
2. Each email sent includes a link to this form, pre-filled with the
   subscriber's email (using Google Forms' prefill URL feature).
3. Publish that Sheet as CSV too, put the URL in
   UNSUBSCRIBE_SHEET_CSV_URL env var / secret.
"""

import os
import csv
import json
import datetime
import requests

GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL")
UNSUBSCRIBE_SHEET_CSV_URL = os.environ.get("UNSUBSCRIBE_SHEET_CSV_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")  # e.g. "shridharsreeram-boop/LinkedinAlerts"
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")


def load_subscribers():
    if not os.path.exists(SUBSCRIBERS_FILE):
        return []
    with open(SUBSCRIBERS_FILE) as f:
        return json.load(f)


def save_subscribers(subs):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subs, f, indent=2)


def fetch_csv_rows(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    reader = csv.DictReader(resp.text.splitlines())
    return [{k.strip(): v.strip() for k, v in row.items()} for row in reader]


def process_signups(subscribers):
    if not GOOGLE_SHEET_CSV_URL:
        print("[warn] GOOGLE_SHEET_CSV_URL not set, skipping signup sync")
        return subscribers, 0, 0

    rows = fetch_csv_rows(GOOGLE_SHEET_CSV_URL)
    by_email = {s["email"]: s for s in subscribers}

    # Keep only the LATEST row per email from the form (Google Forms appends new rows,
    # so a resubmission with the same email means "update my preferences")
    latest_by_email = {}
    for row in rows:
        email = row.get("Email", "").strip()
        if email:
            latest_by_email[email] = row

    added, updated = 0, 0
    for email, row in latest_by_email.items():
        duration_days = int(row.get("Alert Duration (days)", 30) or 30)
        end_date = (datetime.date.today() + datetime.timedelta(days=duration_days)).isoformat()

        new_record = {
            "name": row.get("Name", "there").strip(),
            "email": email,
            "job_title": row.get("Job Title", "").strip(),
            "location": row.get("Location", "").strip(),
            "country_code": row.get("Country Code", "").strip().lower(),
            "end_date": end_date,
            "signed_up": by_email.get(email, {}).get("signed_up", datetime.date.today().isoformat()),
        }

        if email in by_email:
            if by_email[email] != new_record:
                updated += 1
        else:
            added += 1

        by_email[email] = new_record

    return list(by_email.values()), added, updated


def process_unsubscribes(subscribers):
    if not UNSUBSCRIBE_SHEET_CSV_URL:
        print("[warn] UNSUBSCRIBE_SHEET_CSV_URL not set, skipping unsubscribe sync")
        return subscribers, 0

    try:
        rows = fetch_csv_rows(UNSUBSCRIBE_SHEET_CSV_URL)
    except Exception as e:
        print(f"[warn] failed to fetch unsubscribe sheet: {e}")
        return subscribers, 0

    unsubscribe_emails = {row.get("Email", "").strip() for row in rows if row.get("Email", "").strip()}
    if not unsubscribe_emails:
        return subscribers, 0

    before_count = len(subscribers)
    remaining = [s for s in subscribers if s["email"] not in unsubscribe_emails]
    removed = before_count - len(remaining)
    return remaining, removed


def trigger_welcome_run():
    """Fire a repository_dispatch event so the welcome workflow runs immediately
    for the new subscriber, without waiting for the next scheduled cron run."""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("[warn] GITHUB_TOKEN or GITHUB_REPOSITORY not set, skipping welcome run trigger")
        return
    try:
        resp = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/dispatches",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"event_type": "new_subscriber"},
            timeout=15,
        )
        if resp.status_code == 204:
            print("  [info] Welcome run triggered for new subscriber.")
        else:
            print(f"  [warn] Failed to trigger welcome run: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  [warn] Welcome run trigger failed: {e}")


def main():
    subscribers = load_subscribers()

    subscribers, added, updated = process_signups(subscribers)
    subscribers, removed = process_unsubscribes(subscribers)

    save_subscribers(subscribers)
    print(f"Synced signups: {added} new subscriber(s) added, {updated} updated, {removed} unsubscribed.")

    if added > 0:
        trigger_welcome_run()


if __name__ == "__main__":
    main()
