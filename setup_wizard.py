#!/usr/bin/env python3
"""
setup_wizard.py
----------------
Run this LOCALLY on your own machine (never on a shared/public server).

What it does:
1. Prompts you for each required credential
2. Validates each one with a real test API call before accepting it
3. Pushes valid secrets directly into your GitHub repo's secrets store
   using the official GitHub CLI (`gh`) - so the values never touch a
   plaintext file, never get committed, and never pass through any
   third party.
4. Tells you exactly which manual steps remain (things only you can do,
   like clicking through Google Forms / GitHub Pages settings).

Prerequisites (install once, see README):
- GitHub CLI installed and logged in: https://cli.github.com  (`gh auth login`)
- Run this from inside your cloned repo folder, OR pass --repo owner/name

Security notes:
- This script never writes your keys to disk.
- It uses `gh secret set`, which encrypts the value client-side before
  sending it to GitHub - the same mechanism GitHub's own docs recommend.
- Your terminal input is not logged to shell history when using getpass.
"""

import subprocess
import sys
import getpass
import argparse
import requests


def run(cmd, input_text=None):
    """Run a shell command, optionally piping input_text to stdin."""
    result = subprocess.run(
        cmd, input=input_text, capture_output=True, text=True
    )
    return result


def check_gh_cli():
    result = run(["gh", "--version"])
    if result.returncode != 0:
        print("ERROR: GitHub CLI ('gh') not found. Install it from https://cli.github.com")
        sys.exit(1)
    result = run(["gh", "auth", "status"])
    if result.returncode != 0:
        print("ERROR: You're not logged into the GitHub CLI. Run: gh auth login")
        sys.exit(1)
    print("✓ GitHub CLI is installed and authenticated.\n")


def set_secret(repo, name, value):
    result = run(["gh", "secret", "set", name, "--repo", repo, "--body", value])
    if result.returncode == 0:
        print(f"  ✓ Secret '{name}' set successfully on {repo}\n")
    else:
        print(f"  ✗ Failed to set '{name}': {result.stderr.strip()}\n")
    return result.returncode == 0


def validate_adzuna(app_id, app_key):
    try:
        resp = requests.get(
            "https://api.adzuna.com/v1/api/jobs/se/search/1",
            params={"app_id": app_id, "app_key": app_key, "results_per_page": 1},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"  [error] {e}")
        return False


def validate_anthropic(api_key):
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": "claude-sonnet-4-6", "max_tokens": 5,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"  [error] {e}")
        return False


def validate_resend(api_key):
    try:
        # The /domains endpoint just lists your verified domains - safe, free, no email sent
        resp = requests.get(
            "https://api.resend.com/domains",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"  [error] {e}")
        return False


def prompt_and_validate(label, validator, *, secret=True, extra_prompt=None):
    """Loop until the user enters a value that passes validation."""
    while True:
        getter = getpass.getpass if secret else input
        value = getter(f"Enter your {label}: ").strip()
        if not value:
            print("  (skipped - leaving this blank, you can add it later)\n")
            return None
        print(f"  Validating {label}...")
        ok = validator(value) if extra_prompt is None else validator(value, extra_prompt())
        if ok:
            print(f"  ✓ {label} looks valid.\n")
            return value
        else:
            retry = input(f"  ✗ Could not validate {label}. Try again? [y/n]: ").strip().lower()
            if retry != "y":
                return None


def main():
    parser = argparse.ArgumentParser(description="Job Alert Pipeline setup wizard")
    parser.add_argument("--repo", help="GitHub repo as owner/name", required=True)
    args = parser.parse_args()

    print("=" * 60)
    print("Job Alert Pipeline — Setup Wizard")
    print("=" * 60)
    print(f"Target repo: {args.repo}\n")

    check_gh_cli()

    confirm = input(
        "This will read API keys via hidden prompts and push them directly\n"
        "to GitHub Secrets on the repo above. Nothing is saved to disk.\n"
        "Continue? [y/n]: "
    ).strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    print("\n--- Adzuna (job search data) ---")
    adzuna_id = input("Enter your Adzuna App ID: ").strip()
    adzuna_key = prompt_and_validate(
        "Adzuna App Key",
        lambda key: validate_adzuna(adzuna_id, key),
    )

    print("--- Anthropic (Claude API for relevance scoring) ---")
    anthropic_key = prompt_and_validate("Anthropic API key", validate_anthropic)

    print("--- Resend (email delivery) ---")
    resend_key = prompt_and_validate("Resend API key", validate_resend)

    print("--- Sender email ---")
    from_email = input("Enter the FROM email address you verified in Resend: ").strip()

    print("--- Google Sheet CSV URL (from your signup form) ---")
    sheet_url = input("Paste the published CSV URL (or leave blank to add later): ").strip()

    print("\nPushing validated secrets to GitHub...\n")
    if adzuna_id and adzuna_key:
        set_secret(args.repo, "ADZUNA_APP_ID", adzuna_id)
        set_secret(args.repo, "ADZUNA_APP_KEY", adzuna_key)
    if anthropic_key:
        set_secret(args.repo, "ANTHROPIC_API_KEY", anthropic_key)
    if resend_key:
        set_secret(args.repo, "RESEND_API_KEY", resend_key)
    if from_email:
        set_secret(args.repo, "ALERT_FROM_EMAIL", from_email)
    if sheet_url:
        set_secret(args.repo, "GOOGLE_SHEET_CSV_URL", sheet_url)

    print("=" * 60)
    print("Done with what can be automated. Remaining manual steps:")
    print("=" * 60)
    print("""
1. Create your Google Form (Name, Email, Job Title, Location,
   Country Code, Alert Duration) if you haven't already, and link
   it to a Sheet -> publish that Sheet as CSV -> re-run this wizard
   if you skipped the URL above.

2. Update docs/index.html in your repo: replace the placeholder
   Google Form link with your real one.

3. Turn on GitHub Pages:
   Repo -> Settings -> Pages -> Source: branch 'main', folder '/docs'

4. Trigger a test run:
   Repo -> Actions tab -> "Job Alert Pipeline" -> Run workflow

These four steps require clicking through Google/GitHub's own UI and
can't be safely scripted on your behalf - but everything credential-
related is now configured.
""")


if __name__ == "__main__":
    main()
