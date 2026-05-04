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
import textwrap
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
    """Pick a fully-qualified domain path like 'engineering.platform.api-gateway' and an owner email.

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
      .github/workflows/ci.yml  (optional; generator overwrites with a resilient workflow)
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

    # Create the empty repo via the REST API directly. We bypass `gh` because
    # it does a precheck against /users/<owner> that 401s with fine-grained
    # PATs. urllib with the raw token in the Authorization header works.
    token = os.environ.get("GH_TOKEN") or os.environ.get("GH_PAT") or ""
    if not token:
        raise RuntimeError("GH_TOKEN is not set")
    _github_post(
        f"/orgs/{ORG}/repos",
        {"name": name, "private": private, "description": description[:350]},
        token=token,
    )

    remote_url = f"https://x-access-token:{token}@github.com/{ORG}/{name}.git"
    for cmd in (
        ["git", "remote", "add", "origin", remote_url],
        ["git", "push", "-u", "origin", "main"],
    ):
        r = _run(cmd, cwd=workdir)
        if r.returncode != 0:
            # Redact token from any error we surface so it doesn't hit the log.
            err = (r.stderr or r.stdout).strip().replace(token, "***")
            raise RuntimeError(f"{cmd[0]} {cmd[1]} failed: {err}")


def archive_repo(name: str) -> None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GH_PAT") or ""
    if not token:
        log(f"WARN: GH_TOKEN not set; cannot archive {name}")
        return
    try:
        _github_request("PATCH", f"/repos/{ORG}/{name}", {"archived": True}, token=token)
    except Exception as e:
        log(f"WARN: failed to archive {name}: {e}")


def _set_lunar_secret(name: str) -> None:
    """Set LUNAR_HUB_TOKEN on the new repo so its CI workflow's lunar-ci-action
    can authenticate to the cronos hub. Uses the gh CLI which handles libsodium
    encryption against the repo public key."""
    value = os.environ.get("LUNAR_HUB_TOKEN", "")
    if not value:
        log(f"WARN: LUNAR_HUB_TOKEN not set; skipping repo secret for {name}")
        return
    try:
        r = subprocess.run(
            [
                "gh", "secret", "set", "LUNAR_HUB_TOKEN",
                "--repo", f"{ORG}/{name}",
                "--app", "actions",
                "--body", value,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()[:300].replace(value, "***")
            log(f"WARN: gh secret set failed for {name}: {err}")
    except Exception as e:
        log(f"WARN: secret set exception for {name}: {e}")


def _github_post(path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    return _github_request("POST", path, body, token=token)


def _github_request(method: str, path: str, body: dict[str, Any] | None,
                    token: str) -> dict[str, Any]:
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
            "Content-Type": "application/json",
            "User-Agent": "cronos-simulator",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body_text = r.read().decode()
            return json.loads(body_text) if body_text else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"GitHub {method} {path} HTTP {e.code}: {detail[:500]}")


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
        _write_ci_workflow(workdir, arch)

        try:
            create_repo_and_push(name, description, workdir)
        except Exception as e:
            log(f"failed to create {ORG}/{name}: {e}")
            return None

    _set_lunar_secret(name)

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


def _ci_workflow_yaml(arch: Archetype) -> str:
    """GitHub Actions workflow that stays green on thin AI repos but exercises real stacks when present.

    The Lunar CI Agent step runs FIRST so it can attach via ptrace and instrument
    every subsequent command in the job (see docs/install/agent-managed.md)."""
    if arch.language == "docs-only":
        return textwrap.dedent("""
            name: CI
            on:
              push:
                branches: [main]
              pull_request:
                branches: [main]
            permissions:
              contents: read
            jobs:
              validate:
                runs-on: ubuntu-latest
                steps:
                  - name: Run Lunar CI Agent
                    uses: earthly/lunar-ci-action@v1.1.5
                    env:
                      LUNAR_HUB_TOKEN: ${{ secrets.LUNAR_HUB_TOKEN }}
                      LUNAR_HUB_HOST: cronos.demo.earthly.dev

                  - uses: actions/checkout@v4
                  - name: Markdown present
                    shell: bash
                    run: |
                      test -f README.md \\
                        || test -f docs/README.md \\
                        || compgen -G '*.md' >/dev/null \\
                        || find . -name '*.md' -not -path './.git/*' -print -quit | grep -q .
            """).strip() + "\n"

    # Code, yaml-only, and everything else: conditional checks; never fail the job on missing stacks.
    return textwrap.dedent("""
        name: CI
        on:
          push:
            branches: [main]
          pull_request:
            branches: [main]
        concurrency:
          group: ${{ github.workflow }}-${{ github.ref }}
          cancel-in-progress: true
        permissions:
          contents: read
        jobs:
          validate:
            runs-on: ubuntu-latest
            steps:
              - name: Run Lunar CI Agent
                uses: earthly/lunar-ci-action@v1.1.5
                env:
                  LUNAR_HUB_TOKEN: ${{ secrets.LUNAR_HUB_TOKEN }}
                  LUNAR_HUB_HOST: cronos.demo.earthly.dev

              - uses: actions/checkout@v4

              - name: Go
                if: hashFiles('**/go.mod') != ''
                uses: actions/setup-go@v5
                with:
                  go-version: stable
              - name: Go build
                if: hashFiles('**/go.mod') != ''
                shell: bash
                run: |
                  set +e
                  while IFS= read -r mod; do
                    [[ -z "$mod" ]] && continue
                    d=$(dirname "$mod")
                    (cd "$d" && go mod download 2>/dev/null; go build ./... 2>/dev/null || go build . 2>/dev/null) || true
                  done < <(find . -name go.mod ! -path './.git/*' -print)

              - name: Python
                if: hashFiles('**/*.py') != ''
                uses: actions/setup-python@v5
                with:
                  python-version: "3.12"
              - name: Python compileall
                if: hashFiles('**/*.py') != ''
                shell: bash
                run: python -m compileall -q . || true

              - name: Node
                if: hashFiles('**/package.json') != ''
                uses: actions/setup-node@v4
                with:
                  node-version: "20"
              - name: npm
                if: hashFiles('**/package.json') != ''
                shell: bash
                run: |
                  set +e
                  while IFS= read -r pkg; do
                    [[ -z "$pkg" ]] && continue
                    d=$(dirname "$pkg")
                    (
                      cd "$d" || exit 0
                      if [[ -f package-lock.json ]] || [[ -f npm-shrinkwrap.json ]]; then
                        npm ci --ignore-scripts --no-fund --no-audit 2>/dev/null \\
                          || npm install --ignore-scripts --no-fund --no-audit 2>/dev/null || true
                      else
                        npm install --ignore-scripts --no-fund --no-audit 2>/dev/null || true
                      fi
                      npm run test --if-present 2>/dev/null || npm run build --if-present 2>/dev/null || true
                    ) || true
                  done < <(find . -name package.json ! -path './.git/*' ! -path '*/node_modules/*' -print)

              - name: Rust
                if: hashFiles('**/Cargo.toml') != ''
                uses: dtolnay/rust-toolchain@stable
              - name: Cargo check
                if: hashFiles('**/Cargo.toml') != ''
                shell: bash
                run: |
                  set +e
                  while IFS= read -r c; do
                    [[ -z "$c" ]] && continue
                    d=$(dirname "$c")
                    (cd "$d" && cargo check -q) || true
                  done < <(find . -name Cargo.toml ! -path './.git/*' -print)

              - name: Java (Maven)
                if: hashFiles('**/pom.xml') != ''
                uses: actions/setup-java@v4
                with:
                  distribution: temurin
                  java-version: "17"
              - name: mvn validate
                if: hashFiles('**/pom.xml') != ''
                shell: bash
                run: |
                  set +e
                  if ! command -v mvn >/dev/null 2>&1; then
                    sudo apt-get update -qq && sudo apt-get install -y -qq maven >/dev/null 2>&1 || true
                  fi
                  while IFS= read -r pom; do
                    [[ -z "$pom" ]] && continue
                    d=$(dirname "$pom")
                    (cd "$d" && mvn -B -q -DskipTests validate) || true
                  done < <(find . -name pom.xml ! -path './.git/*' -print)

              - name: PHP
                if: hashFiles('**/composer.json') != ''
                shell: bash
                run: |
                  set +e
                  sudo apt-get update -qq \\
                    && sudo apt-get install -y -qq php-cli php-xml composer >/dev/null 2>&1 || true
                  while IFS= read -r c; do
                    [[ -z "$c" ]] && continue
                    d=$(dirname "$c")
                    (cd "$d" && (composer install -n --no-progress 2>/dev/null || true) \\
                      && (composer validate --no-check-publish 2>/dev/null || true)) || true
                  done < <(find . -name composer.json ! -path './.git/*' -print)

              - name: YAML sanity
                if: hashFiles('**/*.yaml') != '' || hashFiles('**/*.yml') != ''
                shell: bash
                run: |
                  set +e
                  python3 -m pip install -q --user pyyaml 2>/dev/null || python3 -m pip install -q pyyaml || true
                  while IFS= read -r f; do
                    [[ -z "$f" ]] && continue
                    python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1],encoding='utf-8'))" "$f" || true
                  done < <(find . \\( -name '*.yaml' -o -name '*.yml' \\) ! -path './.git/*' ! -path '*/node_modules/*' -print | head -80)

              - name: Done
                run: echo "ci ok"
        """).strip() + "\n"


def _write_ci_workflow(workdir: Path, arch: Archetype) -> None:
    path = workdir / ".github" / "workflows" / "ci.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ci_workflow_yaml(arch))


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


def _sync_catalog_info(path: Path, name: str, domain: str, owner: str, lifecycle: str) -> None:
    """Force Backstage domain/owner/name to match the generator (Claude often
    emits only the child segment, e.g. ml.recommendations)."""
    if path.exists():
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            doc = None
    else:
        doc = None
    if not isinstance(doc, dict) or doc.get("kind") != "Component":
        doc = {}
    doc.setdefault("apiVersion", "backstage.io/v1alpha1")
    doc["kind"] = "Component"
    meta = doc.setdefault("metadata", {})
    if not isinstance(meta, dict):
        meta = {}
        doc["metadata"] = meta
    meta["name"] = name
    ann = meta.setdefault("annotations", {})
    if not isinstance(ann, dict):
        ann = {}
        meta["annotations"] = ann
    ann["pantalasa.org/domain"] = domain
    spec = doc.setdefault("spec", {})
    if not isinstance(spec, dict):
        spec = {}
        doc["spec"] = spec
    if not spec.get("type"):
        spec["type"] = "service"
    spec["lifecycle"] = lifecycle
    spec["owner"] = owner
    path.write_text(
        yaml.dump(
            doc,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
    )


def _ensure_minimum_files(workdir: Path, name: str, arch: Archetype,
                          domain: str, owner: str) -> None:
    """Guarantee lunar.yml, catalog-info.yaml and CODEOWNERS exist, even if
    the AI skipped them. README is left to Claude (we don't want to overwrite
    something useful), but we add a stub if missing.

    catalog-info.yaml is always rewritten with the canonical domain/owner so
    model output cannot drift (e.g. ml.recommendations vs engineering.ml.recommendations)."""
    lunar_yml = workdir / "lunar.yml"
    if not lunar_yml.exists():
        lunar_yml.write_text(
            "components:\n"
            f"  github.com/{ORG}/{name}:\n"
            f"    tags: [{arch.language}, {arch.role}, {arch.lifecycle}]\n"
        )
    catalog = workdir / "catalog-info.yaml"
    _sync_catalog_info(catalog, name, domain, owner, arch.lifecycle)
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

    if not os.environ.get("GH_TOKEN"):
        log("WARNING: GH_TOKEN is not set")

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
