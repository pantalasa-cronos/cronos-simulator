#!/usr/bin/env python3
"""Inject the lunar-ci-action step at the top of .github/workflows/ci.yml in
every non-archived simulator repo that doesn't already have it.

Strategy: load the existing ci.yml, find each `jobs.<name>.steps:` list whose
job runs on `ubuntu*` runners, and prepend the standard Lunar CI Agent step
(uses earthly/lunar-ci-action@v1.1.5). Skip the file entirely if the step is
already present anywhere. Skip archived repos (writes return 403).

Usage:
  export GH_TOKEN=...
  python scripts/sweep-ci-yaml.py [--dry-run] [--include-archived] \\
                                  [--org pantalasa-cronos]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

try:
    import yaml
except ImportError:
    print("Install PyYAML: pip install pyyaml", file=sys.stderr)
    raise

LUNAR_ACTION_REF = "earthly/lunar-ci-action@v1.1.5"
HUB_HOST = os.environ.get("LUNAR_HUB_HOST", "cronos.demo.earthly.dev")


def gh_request(method: str, path: str, token: str,
               body: dict | None = None) -> tuple[int, Any, str]:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "cronos-simulator-sweep",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None), ""
    except urllib.error.HTTPError as e:
        return e.code, None, e.read().decode(errors="replace")


def list_repos(org: str, token: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        code, data, err = gh_request(
            "GET", f"/orgs/{org}/repos?per_page=100&page={page}&type=all", token
        )
        if code != 200 or not isinstance(data, list):
            raise SystemExit(f"list repos failed {code}: {err[:300]}")
        if not data:
            break
        for r in data:
            if r.get("name"):
                out.append({
                    "name": r["name"],
                    "archived": bool(r.get("archived")),
                    "default_branch": r.get("default_branch") or "main",
                })
        if len(data) < 100:
            break
        page += 1
    return out


def fetch_ci_yaml(org: str, repo: str, ref: str, token: str) -> tuple[str | None, str | None]:
    path = f"/repos/{org}/{repo}/contents/.github/workflows/ci.yml?ref={quote(ref, safe='')}"
    code, data, _ = gh_request("GET", path, token)
    if code == 404 or not isinstance(data, dict):
        return None, None
    if code != 200:
        return None, None
    b64 = data.get("content")
    sha = data.get("sha")
    if not b64 or not sha:
        return None, None
    raw = base64.b64decode("".join(b64.split())).decode("utf-8")
    return raw, sha


def already_has_agent(yaml_text: str) -> bool:
    return "earthly/lunar-ci-action" in yaml_text


def lunar_step_dict() -> dict:
    return {
        "name": "Run Lunar CI Agent",
        "uses": LUNAR_ACTION_REF,
        "env": {
            "LUNAR_HUB_TOKEN": "${{ secrets.LUNAR_HUB_TOKEN }}",
            "LUNAR_HUB_HOST": HUB_HOST,
        },
    }


def inject_into_doc(doc: Any) -> tuple[Any, int]:
    """Walk the loaded YAML and prepend the agent step into each `jobs.*.steps`
    list whose runner matches `ubuntu*`. Returns (updated_doc, jobs_modified)."""
    modified = 0
    if not isinstance(doc, dict):
        return doc, 0
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return doc, 0
    for _job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        runs_on = job.get("runs-on")
        if isinstance(runs_on, str):
            runners = [runs_on]
        elif isinstance(runs_on, list):
            runners = [r for r in runs_on if isinstance(r, str)]
        else:
            runners = []
        if not any(r.lower().startswith("ubuntu") for r in runners):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        if any(isinstance(s, dict) and isinstance(s.get("uses"), str)
               and s["uses"].startswith("earthly/lunar-ci-action") for s in steps):
            continue
        steps.insert(0, lunar_step_dict())
        modified += 1
    return doc, modified


def put_ci_yaml(org: str, repo: str, ref: str, sha: str, new_body: str,
                token: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"  DRY {repo}: would prepend Lunar CI Agent step", flush=True)
        return True
    encoded = base64.b64encode(new_body.encode("utf-8")).decode("ascii")
    payload = {
        "message": "ci: prepend lunar-ci-action step (hub instrumentation)",
        "content": encoded,
        "sha": sha,
        "branch": ref,
    }
    code, _, err = gh_request(
        "PUT", f"/repos/{org}/{repo}/contents/.github/workflows/ci.yml",
        token, payload,
    )
    if code not in (200, 201):
        print(f"  FAIL {repo}: PUT {code} {err[:300]}", flush=True)
        return False
    print(f"  OK   {repo}", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default=os.environ.get("SIMULATOR_ORG", "pantalasa-cronos"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-archived", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        print("GH_TOKEN required", file=sys.stderr)
        return 1

    repos = list_repos(args.org, token)
    print(f"Repos in {args.org}: {len(repos)}", flush=True)

    ok = fail = skipped = noop = 0
    for r in sorted(repos, key=lambda x: x["name"]):
        if r["archived"] and not args.include_archived:
            skipped += 1
            continue
        text, sha = fetch_ci_yaml(args.org, r["name"], r["default_branch"], token)
        if text is None or sha is None:
            noop += 1  # no ci.yml present
            continue
        if already_has_agent(text):
            noop += 1
            continue
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            print(f"  WARN {r['name']}: invalid YAML ({e}); skipping", flush=True)
            noop += 1
            continue
        new_doc, modified = inject_into_doc(doc)
        if modified == 0:
            noop += 1
            continue
        new_body = yaml.dump(new_doc, default_flow_style=False, sort_keys=False)
        if put_ci_yaml(args.org, r["name"], r["default_branch"], sha, new_body, token, args.dry_run):
            ok += 1
        else:
            fail += 1
        time.sleep(0.05)

    print(
        f"Done. updated={ok} unchanged_or_missing={noop} fail={fail} skipped_archived={skipped} dry_run={args.dry_run}",
        flush=True,
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
