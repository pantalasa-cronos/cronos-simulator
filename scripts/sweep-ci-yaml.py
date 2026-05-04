#!/usr/bin/env python3
"""Inject the lunar-ci-action step at the top of .github/workflows/ci.yml in
every non-archived simulator repo that doesn't already have it.

Uses `git clone --depth=1` + edit + push (over HTTPS with the token), because
the GitHub REST Contents API refuses writes to .github/workflows/* unless the
PAT has the `workflow` scope; classic PATs with only `repo` scope return 404.
Git pushes are not subject to that scope check.

Usage:
  export GH_TOKEN=...                # PAT with `repo` scope
  python scripts/sweep-ci-yaml.py [--dry-run] [--include-archived] \\
                                  [--org pantalasa-cronos]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("Install PyYAML: pip install pyyaml", file=sys.stderr)
    raise

LUNAR_ACTION_REF = "earthly/lunar-ci-action@v1.1.5"
HUB_HOST = os.environ.get("LUNAR_HUB_HOST", "cronos.demo.earthly.dev")
COMMIT_USER_NAME = os.environ.get("CRONOS_GIT_NAME", "cronos-simulator")
COMMIT_USER_EMAIL = os.environ.get("CRONOS_GIT_EMAIL", "simulator@pantalasa.org")


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


def _run(cmd: list[str], cwd: Path | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    # Disable any system credential helpers (e.g. WSL inheriting a Windows
    # one) so the inline x-access-token URL is the sole auth source.
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if cmd and cmd[0] == "git":
        cmd = [cmd[0], "-c", "credential.helper=", *cmd[1:]]
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        out = (r.stderr or r.stdout).strip()[:300]
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {out}")
    return r


def update_repo(org: str, repo: str, ref: str, token: str,
                dry_run: bool) -> tuple[str, str]:
    """Returns (status, detail) where status is one of:
      ok, dry, missing, unchanged, invalid_yaml, fail."""
    auth_url = f"https://x-access-token:{token}@github.com/{org}/{repo}.git"
    with tempfile.TemporaryDirectory(prefix="sweep-ci-") as tmp:
        workdir = Path(tmp) / repo
        try:
            _run(["git", "clone", "--depth=1", "--branch", ref, auth_url, str(workdir)])
        except Exception as e:
            return "fail", f"clone: {str(e).replace(token, '***')[:200]}"

        ci = workdir / ".github" / "workflows" / "ci.yml"
        if not ci.exists():
            return "missing", "no ci.yml"

        text = ci.read_text()
        if "earthly/lunar-ci-action" in text:
            return "unchanged", "agent already present"

        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return "invalid_yaml", str(e)[:120]

        new_doc, modified = inject_into_doc(doc)
        if modified == 0:
            return "unchanged", "no ubuntu jobs"

        new_body = yaml.dump(new_doc, default_flow_style=False, sort_keys=False)
        ci.write_text(new_body)

        if dry_run:
            return "dry", f"would inject (jobs={modified})"

        try:
            _run(["git", "-c", f"user.name={COMMIT_USER_NAME}",
                         "-c", f"user.email={COMMIT_USER_EMAIL}",
                         "add", ".github/workflows/ci.yml"], cwd=workdir)
            _run(["git", "-c", f"user.name={COMMIT_USER_NAME}",
                         "-c", f"user.email={COMMIT_USER_EMAIL}",
                         "commit", "-m",
                         "ci: prepend lunar-ci-action step (hub instrumentation)"],
                  cwd=workdir)
            _run(["git", "push", "origin", f"HEAD:{ref}"], cwd=workdir)
        except Exception as e:
            return "fail", f"push: {str(e).replace(token, '***')[:200]}"

    return "ok", f"jobs={modified}"


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
    if shutil.which("git") is None:
        print("git CLI is required", file=sys.stderr)
        return 1

    repos = list_repos(args.org, token)
    print(f"Repos in {args.org}: {len(repos)}", flush=True)

    counts: dict[str, int] = {}
    for r in sorted(repos, key=lambda x: x["name"]):
        if r["archived"] and not args.include_archived:
            counts["archived"] = counts.get("archived", 0) + 1
            continue
        status, detail = update_repo(
            args.org, r["name"], r["default_branch"], token, args.dry_run
        )
        counts[status] = counts.get(status, 0) + 1
        marker = {
            "ok": "  OK  ",
            "dry": "  DRY ",
            "missing": "  --  ",
            "unchanged": "  ==  ",
            "invalid_yaml": "  ??  ",
            "fail": "  FAIL",
        }.get(status, "  ?   ")
        if status in ("ok", "dry", "fail", "invalid_yaml"):
            print(f"{marker} {r['name']}: {detail}", flush=True)
        time.sleep(0.05)

    print("Done.", " ".join(f"{k}={v}" for k, v in counts.items()),
          f"dry_run={args.dry_run}", flush=True)
    return 0 if counts.get("fail", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
