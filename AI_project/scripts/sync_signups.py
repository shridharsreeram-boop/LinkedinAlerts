#!/usr/bin/env python3
"""
sync_signups.py
----------------
Pulls new signups from a Google Sheet (used as a free form backend for the
signup page in docs/index.html) and merges them into data/subscribers.json.

Setup:
1. Create a Google Form with fields: Name, Email, Job Title, Location,
   Country Code, Alert Duration (days).
2. Link it to a Google Sheet.
3. Publish the sheet to the web as CSV (File > Share > Publish to web > CSV),
   and put that URL in the GOOGLE_SHEET_CSV_URL secret/env var.
"""

import os
import csv
import json
import datetime
import requests

GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL")
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


def main():
    if not GOOGLE_SHEET_CSV_URL:
        print("[warn] GOOGLE_SHEET_CSV_URL not set, nothing to sync")
        return

    resp = requests.get(GOOGLE_SHEET_CSV_URL, timeout=30)
    resp.raise_for_status()
    reader = csv.DictReader(resp.text.splitlines())
    rows = [{k.strip(): v.strip() for k, v in row.items()} for row in reader]

    subscribers = load_subscribers()
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

    save_subscribers(list(by_email.values()))
    print(f"Synced signups: {added} new subscriber(s) added, {updated} updated.")


if __name__ == "__main__":
    main()
