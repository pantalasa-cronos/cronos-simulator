#!/usr/bin/env python3
"""Set LUNAR_HUB_TOKEN as an Actions secret on every non-archived repo in the
simulator org so each component repo's CI workflow can reach the cronos hub.

Uses the gh CLI (which handles libsodium encryption against the repo public
key). One repo at a time so we never see plaintext on the wire ourselves.

Usage:
  export GH_TOKEN=...                # PAT with repo scope (admin not required)
  export LUNAR_HUB_TOKEN=...         # the cronos hub token to write
  python scripts/sweep-repo-secrets.py [--org pantalasa-cronos] \\
                                       [--include-archived] [--dry-run]

Skips archived repos by default (PUT against archived repos returns 403).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ORG_DEFAULT = os.environ.get("SIMULATOR_ORG", "pantalasa-cronos")


def github_get(path: str, token: str):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cronos-simulator-sweep",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def list_repos(org: str, token: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        code, data = github_get(
            f"/orgs/{org}/repos?per_page=100&page={page}&type=all", token
        )
        if code != 200 or not isinstance(data, list):
            raise SystemExit(f"list repos failed {code}: {str(data)[:300]}")
        if not data:
            break
        for r in data:
            if r.get("name"):
                out.append({"name": r["name"], "archived": bool(r.get("archived"))})
        if len(data) < 100:
            break
        page += 1
    return out


def set_secret(org: str, repo: str, value: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"  DRY {repo}: would set LUNAR_HUB_TOKEN", flush=True)
        return True
    r = subprocess.run(
        [
            "gh", "secret", "set", "LUNAR_HUB_TOKEN",
            "--repo", f"{org}/{repo}",
            "--app", "actions",
            "--body", value,
        ],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()[:300].replace(value, "***")
        print(f"  FAIL {repo}: {err}", flush=True)
        return False
    print(f"  OK   {repo}", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default=ORG_DEFAULT)
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if shutil.which("gh") is None:
        print("gh CLI is required", file=sys.stderr)
        return 1

    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not gh_token:
        print("GH_TOKEN required (PAT with repo scope)", file=sys.stderr)
        return 1

    secret_value = os.environ.get("LUNAR_HUB_TOKEN", "")
    if not secret_value:
        print("LUNAR_HUB_TOKEN required (the cronos hub token to write)", file=sys.stderr)
        return 1

    repos = list_repos(args.org, gh_token)
    print(f"Repos in {args.org}: {len(repos)}", flush=True)

    ok = fail = skipped = 0
    for r in sorted(repos, key=lambda x: x["name"]):
        if r["archived"] and not args.include_archived:
            skipped += 1
            continue
        if set_secret(args.org, r["name"], secret_value, args.dry_run):
            ok += 1
        else:
            fail += 1
        time.sleep(0.05)  # gentle pacing

    print(
        f"Done. ok={ok} fail={fail} skipped_archived={skipped} dry_run={args.dry_run}",
        flush=True,
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
