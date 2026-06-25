#!/usr/bin/env python3
"""Generates docs/dashboard.html from data/run_log.json so anyone can see
the pipeline's recent activity (no personal data like full emails shown)."""

import os
import json
import datetime

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DOCS_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
LOG_FILE = os.path.join(DATA_DIR, "run_log.json")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")


def mask_email(email):
    name, _, domain = email.partition("@")
    if len(name) <= 2:
        masked = name[0] + "*"
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"


def main():
    log = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            log = json.load(f)

    subscriber_count = 0
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE) as f:
            subscriber_count = len(json.load(f))

    rows = ""
    for run in reversed(log[-20:]):
        ts = run.get("timestamp", "")
        for r in run.get("results", []):
            rows += f"""
            <tr>
              <td>{ts}</td>
              <td>{mask_email(r.get('email',''))}</td>
              <td>{r.get('status','')}</td>
              <td>{r.get('new_jobs_found','-')}</td>
              <td>{r.get('relevant_jobs_sent','-')}</td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Job Alert Pipeline - Dashboard</title>
<style>
  body {{ font-family: Arial, sans-serif; max-width: 900px; margin: 40px auto; color: #222; }}
  h1 {{ font-size: 24px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #e0e0e0; font-size: 14px; }}
  th {{ background: #f5f5f5; }}
  .stat {{ display:inline-block; margin-right: 24px; font-size: 14px; color: #555; }}
  .stat b {{ font-size: 20px; display:block; color:#111; }}
</style>
</head>
<body>
  <h1>Job Alert Pipeline Dashboard</h1>
  <div class="stat"><b>{subscriber_count}</b>Active subscribers</div>
  <div class="stat"><b>{len(log)}</b>Total pipeline runs logged</div>
  <p style="color:#888;font-size:13px;">Last updated: {datetime.datetime.utcnow().isoformat()} UTC</p>
  <table>
    <thead>
      <tr><th>Run Time (UTC)</th><th>Subscriber</th><th>Status</th><th>New Jobs Found</th><th>Relevant Sent</th></tr>
    </thead>
    <tbody>
      {rows if rows else '<tr><td colspan="5">No runs logged yet.</td></tr>'}
    </tbody>
  </table>
</body>
</html>"""

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(os.path.join(DOCS_DIR, "dashboard.html"), "w") as f:
        f.write(html)
    print("Dashboard generated.")


if __name__ == "__main__":
    main()
