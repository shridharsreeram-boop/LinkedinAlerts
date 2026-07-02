#!/usr/bin/env python3
"""Generates docs/dashboard.html from data/run_log.json and data/subscribers.json.
Shows active subscribers, pipeline run history, jobs sent per day chart,
and per-subscriber stats. No personal data exposed — emails are masked."""

import os
import json
import datetime
from collections import defaultdict

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

    subscribers = []
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE) as f:
            subscribers = json.load(f)

    subscriber_count = len(subscribers)
    total_runs = len(log)
    total_sent = sum(
        r.get("relevant_jobs_sent", 0)
        for run in log
        for r in run.get("results", [])
    )

    # Jobs sent per day for chart (last 14 days)
    jobs_by_day = defaultdict(int)
    for run in log:
        ts = run.get("timestamp", "")[:10]
        for r in run.get("results", []):
            jobs_by_day[ts] += r.get("relevant_jobs_sent", 0)

    today = datetime.date.today()
    last_14 = [(today - datetime.timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    chart_labels = [d[5:] for d in last_14]  # MM-DD format
    chart_values = [jobs_by_day.get(d, 0) for d in last_14]
    max_val = max(chart_values) if any(chart_values) else 1

    # Bar chart SVG
    bar_width = 30
    bar_gap = 8
    chart_w = len(last_14) * (bar_width + bar_gap)
    chart_h = 120
    bars_svg = ""
    for i, (label, val) in enumerate(zip(chart_labels, chart_values)):
        bar_h = int((val / max_val) * chart_h) if max_val else 0
        x = i * (bar_width + bar_gap)
        y = chart_h - bar_h
        bars_svg += f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_h}" rx="3" fill="#0ea5e9" opacity="0.8"/>'
        bars_svg += f'<text x="{x + bar_width//2}" y="{chart_h + 14}" text-anchor="middle" font-size="9" fill="#64748b">{label}</text>'
        if val > 0:
            bars_svg += f'<text x="{x + bar_width//2}" y="{y - 3}" text-anchor="middle" font-size="9" fill="#0ea5e9">{val}</text>'

    # Recent runs table rows
    rows = ""
    for run in reversed(log[-30:]):
        ts = run.get("timestamp", "")[:16].replace("T", " ")
        for r in run.get("results", []):
            email = mask_email(r.get("email", ""))
            status = r.get("status", "")
            found = r.get("new_jobs_found", "-")
            sent = r.get("relevant_jobs_sent", "-")
            status_color = "#22c55e" if status == "sent" else "#94a3b8"
            rows += f"""
            <tr style="border-bottom:1px solid #f1f5f9;">
              <td style="padding:8px 12px;font-size:13px;color:#475569;">{ts}</td>
              <td style="padding:8px 12px;font-size:13px;color:#475569;">{email}</td>
              <td style="padding:8px 12px;">
                <span style="font-size:12px;color:{status_color};font-weight:500;">{status}</span>
              </td>
              <td style="padding:8px 12px;font-size:13px;color:#475569;text-align:center;">{found}</td>
              <td style="padding:8px 12px;font-size:13px;color:#0ea5e9;text-align:center;font-weight:500;">{sent}</td>
            </tr>"""

    # Active subscribers list
    sub_rows = ""
    for s in subscribers:
        email = mask_email(s.get("email", ""))
        title = s.get("job_title", "")[:40]
        location = s.get("location", "")
        end_date = s.get("end_date", "")
        sub_rows += f"""
        <tr style="border-bottom:1px solid #f1f5f9;">
          <td style="padding:8px 12px;font-size:13px;color:#475569;">{email}</td>
          <td style="padding:8px 12px;font-size:13px;color:#475569;">{title}</td>
          <td style="padding:8px 12px;font-size:13px;color:#475569;">{location}</td>
          <td style="padding:8px 12px;font-size:13px;color:#94a3b8;">{end_date}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>jobpingapp — Pipeline Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8fafc; color: #1a2332; }}
  .header {{ background: #1a2332; padding: 20px 32px; display: flex; align-items: center; justify-content: space-between; }}
  .header h1 {{ color: #fff; font-size: 18px; font-weight: 700; }}
  .header span {{ color: #94a3b8; font-size: 12px; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px; }}
  .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }}
  .stat {{ background: #fff; border-radius: 10px; padding: 20px 24px; border: 1px solid #e2e8f0; }}
  .stat .value {{ font-size: 32px; font-weight: 700; color: #0ea5e9; }}
  .stat .label {{ font-size: 13px; color: #64748b; margin-top: 4px; }}
  .card {{ background: #fff; border-radius: 10px; border: 1px solid #e2e8f0; margin-bottom: 24px; overflow: hidden; }}
  .card-header {{ padding: 16px 20px; border-bottom: 1px solid #f1f5f9; font-size: 14px; font-weight: 600; color: #1a2332; }}
  .chart-wrap {{ padding: 20px 20px 8px; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ padding: 10px 12px; text-align: left; font-size: 12px; color: #94a3b8; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #e2e8f0; }}
  .footer {{ text-align: center; color: #94a3b8; font-size: 12px; padding: 24px; }}
  a {{ color: #0ea5e9; text-decoration: none; }}
</style>
</head>
<body>

<div class="header">
  <h1>⚡ jobpingapp — Pipeline Dashboard</h1>
  <span>Last updated: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC</span>
</div>

<div class="container">

  <div class="stats">
    <div class="stat">
      <div class="value">{subscriber_count}</div>
      <div class="label">Active subscribers</div>
    </div>
    <div class="stat">
      <div class="value">{total_runs}</div>
      <div class="label">Total pipeline runs</div>
    </div>
    <div class="stat">
      <div class="value">{total_sent}</div>
      <div class="label">Total jobs sent</div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">Jobs sent — last 14 days</div>
    <div class="chart-wrap">
      <svg width="{chart_w}" height="{chart_h + 24}" viewBox="0 0 {chart_w} {chart_h + 24}">
        {bars_svg}
      </svg>
    </div>
  </div>

  <div class="card">
    <div class="card-header">Active subscribers</div>
    <table>
      <thead>
        <tr>
          <th>Email</th><th>Job title(s)</th><th>Location(s)</th><th>Expires</th>
        </tr>
      </thead>
      <tbody>
        {sub_rows if sub_rows else '<tr><td colspan="4" style="padding:16px;color:#94a3b8;text-align:center;">No active subscribers</td></tr>'}
      </tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-header">Recent pipeline runs (last 30)</div>
    <table>
      <thead>
        <tr>
          <th>Time (UTC)</th><th>Subscriber</th><th>Status</th><th style="text-align:center;">Found</th><th style="text-align:center;">Sent</th>
        </tr>
      </thead>
      <tbody>
        {rows if rows else '<tr><td colspan="5" style="padding:16px;color:#94a3b8;text-align:center;">No runs logged yet</td></tr>'}
      </tbody>
    </table>
  </div>

</div>

<div class="footer">
  <a href="https://jobpingapp.xyz">jobpingapp.xyz</a> &nbsp;·&nbsp;
  <a href="https://github.com/shridharsreeram-boop/LinkedinAlerts">GitHub</a>
</div>

</body>
</html>"""

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(os.path.join(DOCS_DIR, "dashboard.html"), "w") as f:
        f.write(html)
    print("Dashboard generated.")


if __name__ == "__main__":
    main()
