#!/usr/bin/env python3
"""Fix pantalasa.org/domain in catalog-info.yaml when it is a short child path.

Reads valid FQ domains from seed/company.json (engineering.*, product.*,
solutions.*). For each org repo that has catalog-info.yaml, if the annotation
is not already valid but exactly one of engineering.{d}, product.{d},
solutions.{d} is valid, update the file via the GitHub Contents API.

Usage:
  export GH_TOKEN=...   # classic PAT: repo scope for org
  python scripts/sweep-catalog-domains.py [--dry-run] [--org pantalasa-cronos]

Requires: PyYAML (same as gen-repo.py).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Install PyYAML: pip install pyyaml", file=sys.stderr)
    raise

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
COMPANY_FILE = REPO_ROOT / "seed" / "company.json"


def fq_domains_from_company(company: dict) -> set[str]:
    out: set[str] = set()
    domains = company.get("domains") or {}
    for top, body in domains.items():
        if not isinstance(body, dict):
            continue
        for sub in body.get("children") or []:
            out.add(f"{top}.{sub}")
    return out


def github_request(
    method: str,
    path: str,
    token: str,
    body: dict | None = None,
) -> tuple[int, dict | list | None, str]:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cronos-simulator-sweep",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            payload = json.loads(raw) if raw else None
            return r.status, payload, ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        return e.code, None, detail


def list_org_repos(org: str, token: str) -> list[str]:
    names: list[str] = []
    page = 1
    while True:
        path = f"/orgs/{org}/repos?per_page=100&page={page}&type=all"
        code, data, err = github_request("GET", path, token)
        if code != 200 or not isinstance(data, list):
            raise SystemExit(f"list repos failed {code}: {err[:500]}")
        if not data:
            break
        for repo in data:
            if isinstance(repo, dict) and repo.get("name"):
                names.append(repo["name"])
        if len(data) < 100:
            break
        page += 1
    return names


def fetch_catalog_info(org: str, repo: str, token: str) -> tuple[str | None, str | None]:
    """Returns (domain_value, file_sha) or (None, None) if missing."""
    path = f"/repos/{org}/{repo}/contents/catalog-info.yaml?ref=main"
    code, data, err = github_request("GET", path, token)
    if code == 404:
        return None, None
    if code != 200 or not isinstance(data, dict):
        print(f"  WARN {repo}: GET catalog-info {code} {err[:200]}", flush=True)
        return None, None
    b64 = data.get("content")
    sha = data.get("sha")
    if not b64 or not sha:
        return None, None
    raw = base64.b64decode("".join(b64.split())).decode("utf-8")
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError:
        print(f"  WARN {repo}: invalid YAML", flush=True)
        return None, None
    if not isinstance(doc, dict):
        return None, None
    meta = doc.get("metadata") or {}
    ann = meta.get("annotations") or {}
    if not isinstance(ann, dict):
        return None, None
    d = ann.get("pantalasa.org/domain")
    if not isinstance(d, str) or not d.strip():
        return None, None
    return d.strip(), sha


def _tie_break(matches: list[str]) -> str:
    for prefix in ("engineering", "product", "solutions"):
        for m in matches:
            if m.startswith(prefix + "."):
                return m
    return matches[0]


def _by_suffix(suffix: str, valid: set[str]) -> list[str]:
    return [v for v in valid if v.endswith("." + suffix)]


def canonical_domain(current: str, valid: set[str]) -> str | None:
    """Return replacement FQ domain, or None if already valid / unmappable."""
    if current in valid:
        return None
    # e.g. ml.recommendations -> engineering.ml.recommendations
    m = _by_suffix(current, valid)
    if len(m) == 1:
        return m[0]
    if len(m) > 1:
        return _tie_break(m)
    # Wrong top-level (e.g. product.merchant-dashboard): strip first segment.
    if "." in current:
        rest = current.split(".", 1)[1]
        m2 = _by_suffix(rest, valid)
        if len(m2) == 1:
            return m2[0]
        if len(m2) > 1:
            return _tie_break(m2)
    # Single-segment child (e.g. checkout -> engineering.checkout).
    candidates = [f"{p}.{current}" for p in ("engineering", "product", "solutions")]
    hits = [c for c in candidates if c in valid]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return _tie_break(hits)
    return None


def put_catalog_info(
    org: str,
    repo: str,
    token: str,
    new_domain: str,
    sha: str,
    dry_run: bool,
) -> bool:
    path = f"/repos/{org}/{repo}/contents/catalog-info.yaml"
    code, data, err = github_request("GET", path + "?ref=main", token)
    if code != 200 or not isinstance(data, dict):
        print(f"  FAIL {repo}: re-fetch {code}", flush=True)
        return False
    b64 = data.get("content")
    if not b64:
        return False
    raw = base64.b64decode("".join(b64.split())).decode("utf-8")
    doc = yaml.safe_load(raw)
    if not isinstance(doc, dict):
        return False
    meta = doc.setdefault("metadata", {})
    if not isinstance(meta, dict):
        meta = {}
        doc["metadata"] = meta
    ann = meta.setdefault("annotations", {})
    if not isinstance(ann, dict):
        ann = {}
        meta["annotations"] = ann
    ann["pantalasa.org/domain"] = new_domain
    new_body = yaml.dump(
        doc,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    encoded = base64.b64encode(new_body.encode("utf-8")).decode("ascii")
    payload = {
        "message": f"fix(catalog): normalize domain to {new_domain}",
        "content": encoded,
        "sha": sha,
    }
    if dry_run:
        print(f"  DRY {repo}: would set domain -> {new_domain}", flush=True)
        return True
    code2, _, err2 = github_request("PUT", path, token, payload)
    if code2 not in (200, 201):
        print(f"  FAIL {repo}: PUT {code2} {err2[:400]}", flush=True)
        return False
    print(f"  OK  {repo}: {new_domain}", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default=os.environ.get("SIMULATOR_ORG", "pantalasa-cronos"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        print("GH_TOKEN or GITHUB_TOKEN required", file=sys.stderr)
        return 1

    if not COMPANY_FILE.exists():
        print(f"{COMPANY_FILE} missing", file=sys.stderr)
        return 1

    with COMPANY_FILE.open() as f:
        company = json.load(f)
    valid = fq_domains_from_company(company)
    print(f"Valid FQ domains: {len(valid)} (from {COMPANY_FILE.name})", flush=True)

    repos = list_org_repos(args.org, token)
    print(f"Repos in {args.org}: {len(repos)}", flush=True)

    fixed = 0
    skipped = 0
    for name in sorted(repos):
        cur, sha = fetch_catalog_info(args.org, name, token)
        if cur is None or sha is None:
            skipped += 1
            continue
        new_d = canonical_domain(cur, valid)
        if new_d is None:
            if cur not in valid:
                print(f"  ??  {name}: domain={cur!r} (no single prefix match)", flush=True)
            skipped += 1
            continue
        if put_catalog_info(args.org, name, token, new_d, sha, args.dry_run):
            fixed += 1

    print(f"Done. updated={fixed} skipped_or_ok={skipped} dry_run={args.dry_run}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
