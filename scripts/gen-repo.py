#!/usr/bin/env python3
"""Create N AI-authored repos in the pantalasa-cronos GitHub org.

For each repo we:
  1. Sample an archetype (language x role x lifecycle) from seed/archetypes.yml.
  2. Pick a domain + owner from seed/company.json.
  3. Invoke Claude Code (claude CLI) in an empty temp dir with a prompt that
     includes prior-repos context so it picks a plausible, non-duplicate name
     and writes minimal-but-real code + lunar.yml + catalog-info.yaml.
  4. `gh repo create pantalasa-cronos/<name> --private --source=. --push`.
  5. Optionally `gh repo archive` if archetype.lifecycle == "archived".
  6. Append the repo to state/repos.jsonl (the ledger).

Design notes:
  * The only mandatory environment is GH_TOKEN and ANTHROPIC_API_KEY.
  * Pure stdlib + PyYAML. PyYAML is tiny and ships on stock Actions runners.
  * Idempotent: self-stops when the ledger already contains TARGET_REPOS entries.
  * Resilient: per-repo errors are logged but don't abort the whole batch.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("Missing PyYAML. In GitHub Actions, add 'pip install pyyaml'.", file=sys.stderr)
    raise

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from pareto import weighted_choice, pareto_tier  # noqa: E402

SEED_DIR = REPO_ROOT / "seed"
STATE_DIR = REPO_ROOT / "state"
LEDGER = STATE_DIR / "repos.jsonl"
ARCHETYPES_FILE = SEED_DIR / "archetypes.yml"
COMPANY_FILE = SEED_DIR / "company.json"

ORG = os.environ.get("SIMULATOR_ORG", "pantalasa-cronos")
TARGET_REPOS = int(os.environ.get("TARGET_REPOS", "1000"))
SLEEP_MIN = float(os.environ.get("SLEEP_MIN_SECONDS", "30"))
SLEEP_MAX = float(os.environ.get("SLEEP_MAX_SECONDS", "60"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "16000"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


@dataclass
class Archetype:
    language: str
    role: str
    lifecycle: str


@dataclass
class RepoRecord:
    name: str
    archetype: Archetype
    activity_tier: str
    domain: str
    owner: str
    created_at: str  # iso8601 utc
    last_commit_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        data = asdict(self)
        return json.dumps(data, separators=(",", ":"))


def log(msg: str) -> None:
    print(f"[gen-repo] {msg}", flush=True)


def load_archetypes() -> dict[str, Any]:
    with ARCHETYPES_FILE.open() as f:
        return yaml.safe_load(f)


def load_company() -> dict[str, Any]:
    if not COMPANY_FILE.exists():
        raise SystemExit(
            f"{COMPANY_FILE} is missing. Run the seed-company workflow first."
        )
    with COMPANY_FILE.open() as f:
        return json.load(f)


def load_ledger() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    with LEDGER.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def append_ledger(record: RepoRecord) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(record.to_jsonl() + "\n")


def sample_archetype(cfg: dict[str, Any], rng: random.Random) -> Archetype:
    return Archetype(
        language=weighted_choice(cfg["languages"], rng=rng),
        role=weighted_choice(cfg["roles"], rng=rng),
        lifecycle=weighted_choice(cfg["lifecycles"], rng=rng),
    )


def pick_domain_and_owner(company: dict[str, Any], rng: random.Random) -> tuple[str, str]:
    """Pick a fully-qualified domain path like 'platform.api-gateway' and an owner email.

    Supports the seed shape documented in the plan:
        {
          "domains": { "<top>": {"owner": "...", "children": ["<sub>", ...] } },
          "people":  [ {"email": "...", "domains": ["top.sub", ...]} ]
        }
    """
    domains = company.get("domains", {})
    if not domains:
        raise SystemExit("seed/company.json has no 'domains' — re-run seed step")

    top = rng.choice(list(domains.keys()))
    children = domains[top].get("children") or []
    if children:
        sub = rng.choice(children)
        fq = f"{top}.{sub}"
    else:
        fq = top

    # Find candidate people for this exact domain, falling back to the top-level.
    people = company.get("people", [])
    candidates = [p["email"] for p in people if fq in p.get("domains", [])]
    if not candidates:
        candidates = [p["email"] for p in people if top in p.get("domains", [])]
    if candidates:
        owner = rng.choice(candidates)
    else:
        owner = domains[top].get("owner", f"{top}-lead@pantalasa.org")
    return fq, owner


def compact_prior_repos(ledger: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    """Return up to ``limit`` recent repos in a compact form for the AI prompt."""
    recent = ledger[-limit:]
    return [
        {
            "name": r["name"],
            "language": r["archetype"]["language"],
            "role": r["archetype"]["role"],
            "lifecycle": r["archetype"]["lifecycle"],
            "domain": r.get("domain"),
        }
        for r in recent
    ]


def fallback_name(arch: Archetype, domain: str, archetypes_cfg: dict[str, Any],
                  rng: random.Random, existing: set[str]) -> str:
    """Deterministic-ish name generator used when Claude does not supply one."""
    team = domain.split(".")[-1]
    suffixes = archetypes_cfg["name_suffixes"].get(arch.role, ["svc"])
    nouns = archetypes_cfg["name_nouns"]
    for _ in range(40):
        noun = rng.choice(nouns)
        suffix = rng.choice(suffixes)
        candidate = f"{team}-{noun}-{suffix}"
        if candidate not in existing:
            return candidate
    return f"{team}-{rng.randint(1000, 9999)}-svc"


def build_claude_prompt(arch: Archetype, domain: str, owner: str, suggested_name: str,
                        prior: list[dict[str, Any]]) -> str:
    prior_json = json.dumps(prior, indent=2) if prior else "[]"
    return f"""You are generating ONE small GitHub repository for a load test.
Return a single JSON object with this exact shape and nothing else:

{{
  "name": "<kebab-case repo name, 3-60 chars, [a-z0-9-]>",
  "description": "<one-line description>",
  "files": {{
    "<relative/path>": "<full file contents as a string>",
    ...
  }}
}}

Company context:
  organization: {ORG}
  assigned domain: {domain}
  assigned owner email: {owner}

Archetype for this repo:
  language: {arch.language}
  role: {arch.role}
  lifecycle: {arch.lifecycle}

Suggested repo name: {suggested_name}
(You MAY override this if a different name fits the company style better.)

Recent repositories in this company (use as naming + thematic context; do NOT duplicate):
{prior_json}

Requirements for "files":
  - 5 to 15 files total. Realistic, minimal, and matching the archetype.
  - Always include these paths:
      README.md                 (Description, Installation, Usage sections)
      .gitignore                (language-appropriate)
      CODEOWNERS                (single line: "* {owner}")
      lunar.yml                 (see template below)
      catalog-info.yaml         (see template below)
      .github/workflows/ci.yml  (one small job, e.g. lint or test)
  - If role is "api-service" or "worker" and lifecycle is "production",
    also include a minimal Dockerfile.
  - If language is "docs-only", skip code and use only Markdown.
  - If language is "yaml-only", generate k8s or helm yaml instead of code.

Template for lunar.yml (substitute the final repo name for <REPO_NAME>):
components:
  github.com/{ORG}/<REPO_NAME>:
    tags: [{arch.language}, {arch.role}, {arch.lifecycle}]

Template for catalog-info.yaml (substitute the final repo name for <REPO_NAME>):
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: <REPO_NAME>
  annotations:
    pantalasa.org/domain: {domain}
spec:
  type: service
  lifecycle: {arch.lifecycle}
  owner: {owner}

Respond with ONLY the JSON object. No markdown fences, no prose.
"""


def run_claude(prompt: str, workdir: Path) -> dict[str, Any]:
    """Call the Anthropic REST API and write the returned files into ``workdir``.

    Returns a dict with ``name``, ``description``, and ``files`` (the parsed
    JSON from the model). Raises RuntimeError on unrecoverable failures.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    req = {
        "model": _resolve_model(CLAUDE_MODEL),
        "max_tokens": CLAUDE_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    log(f"invoking anthropic REST (model={req['model']}, max_tokens={req['max_tokens']})")

    body = json.dumps(req).encode()
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as r:
            resp_body = r.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"anthropic API HTTP {e.code}: {detail[:2000]}")
    except Exception as e:
        raise RuntimeError(f"anthropic API call failed: {e}")

    resp = json.loads(resp_body)
    text = "".join(b["text"] for b in resp.get("content", []) if b.get("type") == "text").strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)

    obj = _parse_first_json_object(text)
    if "name" not in obj:
        raise RuntimeError(f"response missing 'name': keys={list(obj.keys())}")
    files = obj.get("files") or {}
    if not isinstance(files, dict):
        raise RuntimeError(f"response.files is not an object: got {type(files).__name__}")

    name = obj["name"].strip().lower()
    for path, content in files.items():
        if not isinstance(content, str):
            continue
        content = content.replace("<REPO_NAME>", name)
        full = (workdir / path).resolve()
        if workdir.resolve() not in full.parents and full != workdir.resolve():
            raise RuntimeError(f"file path escapes workdir: {path}")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return obj


def _parse_first_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start == -1:
        raise RuntimeError(f"no JSON object in response: {text[:500]!r}")
    depth, end = 0, None
    in_string = False
    esc = False
    for i, ch in enumerate(text[start:], start=start):
        if in_string:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise RuntimeError(f"unbalanced JSON in response: starts {text[start:start+300]!r}")
    return json.loads(text[start:end])


def _resolve_model(alias: str) -> str:
    mapping = {
        "opus": "claude-opus-4-5",
        "sonnet": "claude-sonnet-4-5",
        "haiku": "claude-haiku-4-5",
    }
    return mapping.get(alias, alias)


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=env or os.environ.copy(),
        check=False,
    )


def create_repo_and_push(name: str, description: str, workdir: Path, private: bool = True) -> None:
    visibility = "--private" if private else "--public"
    # Git init + initial commit.
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "-c", "user.name=cronos-simulator",
                "-c", "user.email=simulator@pantalasa.org",
                "commit", "-m", "chore: initial commit"],
    ):
        r = _run(cmd, cwd=workdir)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")

    r = _run(
        ["gh", "repo", "create", f"{ORG}/{name}", visibility,
         "--description", description[:350],
         "--source=.", "--push", "--remote=origin"],
        cwd=workdir,
    )
    if r.returncode != 0:
        raise RuntimeError(f"gh repo create failed: {r.stderr.strip()}")


def archive_repo(name: str) -> None:
    r = _run(["gh", "repo", "archive", f"{ORG}/{name}", "--yes"])
    if r.returncode != 0:
        log(f"WARN: failed to archive {name}: {r.stderr.strip()}")


def existing_repo_names(ledger: list[dict[str, Any]]) -> set[str]:
    return {r["name"] for r in ledger}


def generate_one(cfg: dict[str, Any], company: dict[str, Any],
                 ledger: list[dict[str, Any]], rng: random.Random) -> RepoRecord | None:
    arch = sample_archetype(cfg, rng)
    domain, owner = pick_domain_and_owner(company, rng)
    existing = existing_repo_names(ledger)
    suggested = fallback_name(arch, domain, cfg, rng, existing)
    tier = pareto_tier(rng=rng, shares=cfg.get("activity_tiers"))

    prior = compact_prior_repos(ledger, limit=30)
    prompt = build_claude_prompt(arch, domain, owner, suggested, prior)

    with tempfile.TemporaryDirectory(prefix="cronos-sim-") as tmp:
        workdir = Path(tmp)
        try:
            result = run_claude(prompt, workdir)
        except Exception as e:
            log(f"claude failed ({e}); falling back to name={suggested}")
            result = {"name": suggested,
                      "description": f"{arch.role} in {domain}"}

        name = (result.get("name") or suggested).strip().lower()
        if not _is_valid_name(name) or name in existing:
            log(f"claude suggested invalid/duplicate name '{name}'; using fallback '{suggested}'")
            name = suggested
        description = result.get("description") or f"{arch.role} in {domain}"

        _rewrite_placeholders(workdir, name)
        _ensure_minimum_files(workdir, name, arch, domain, owner)

        try:
            create_repo_and_push(name, description, workdir)
        except Exception as e:
            log(f"failed to create {ORG}/{name}: {e}")
            return None

    if arch.lifecycle == "archived":
        archive_repo(name)

    record = RepoRecord(
        name=name,
        archetype=arch,
        activity_tier=tier,
        domain=domain,
        owner=owner,
        created_at=_utc_now(),
        meta={"description": description},
    )
    append_ledger(record)
    log(f"created {ORG}/{name} ({arch.language}/{arch.role}/{arch.lifecycle}, tier={tier})")
    return record


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_valid_name(name: str) -> bool:
    if not 3 <= len(name) <= 60:
        return False
    return all(c.isalnum() or c == "-" for c in name) and not name.startswith("-") and not name.endswith("-")


def _rewrite_placeholders(workdir: Path, name: str) -> None:
    """Replace literal '<REPO_NAME>' placeholders Claude may have left in files."""
    for p in workdir.rglob("*"):
        if p.is_file() and p.stat().st_size < 200_000:
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            if "<REPO_NAME>" in text:
                p.write_text(text.replace("<REPO_NAME>", name))


def _ensure_minimum_files(workdir: Path, name: str, arch: Archetype,
                          domain: str, owner: str) -> None:
    """Guarantee lunar.yml, catalog-info.yaml and CODEOWNERS exist, even if
    the AI skipped them. README is left to Claude (we don't want to overwrite
    something useful), but we add a stub if missing."""
    lunar_yml = workdir / "lunar.yml"
    if not lunar_yml.exists():
        lunar_yml.write_text(
            "components:\n"
            f"  github.com/{ORG}/{name}:\n"
            f"    tags: [{arch.language}, {arch.role}, {arch.lifecycle}]\n"
        )
    catalog = workdir / "catalog-info.yaml"
    if not catalog.exists():
        catalog.write_text(
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            f"  name: {name}\n"
            "  annotations:\n"
            f"    pantalasa.org/domain: {domain}\n"
            "spec:\n"
            "  type: service\n"
            f"  lifecycle: {arch.lifecycle}\n"
            f"  owner: {owner}\n"
        )
    codeowners = workdir / "CODEOWNERS"
    if not codeowners.exists():
        codeowners.write_text(f"* {owner}\n")
    readme = workdir / "README.md"
    if not readme.exists():
        readme.write_text(
            f"# {name}\n\n"
            f"{arch.role} ({arch.language}) — part of the {domain} domain.\n\n"
            "## Installation\n\nTODO\n\n## Usage\n\nTODO\n\n"
            "## Contributing\n\nSee CODEOWNERS for maintainers.\n"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1, help="how many repos to create this run")
    ap.add_argument("--force", action="store_true", help="ignore TARGET_REPOS self-stop")
    ap.add_argument("--seed", type=int, default=None, help="random seed (for reproducibility)")
    args = ap.parse_args()

    rng = random.Random(args.seed) if args.seed is not None else random.Random()

    archetypes_cfg = load_archetypes()
    company = load_company()
    ledger = load_ledger()

    if not args.force and len(ledger) >= TARGET_REPOS:
        log(f"ledger size {len(ledger)} >= TARGET_REPOS={TARGET_REPOS}; exiting cleanly")
        return 0

    budget = args.count
    if not args.force:
        budget = min(budget, TARGET_REPOS - len(ledger))
    log(f"creating up to {budget} repo(s); ledger size before = {len(ledger)}")

    created = 0
    for i in range(budget):
        try:
            rec = generate_one(archetypes_cfg, company, ledger, rng)
            if rec is not None:
                ledger.append(json.loads(rec.to_jsonl()))
                created += 1
        except Exception as e:
            log(f"unhandled error on iteration {i}: {e}")
        if i < budget - 1:
            pause = rng.uniform(SLEEP_MIN, SLEEP_MAX)
            log(f"sleeping {pause:.1f}s before next repo")
            time.sleep(pause)
    log(f"done: created {created}/{budget} repos; ledger size after = {len(ledger)}")
    return 0 if created > 0 or budget == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
