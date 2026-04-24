#!/usr/bin/env python3
"""Push Pareto-weighted synthetic commits across the simulated repo fleet.

Reads ``state/repos.jsonl`` (populated by gen-repo.py), samples up to
``--commits`` eligible repos according to activity tier, and pushes a tiny
CHANGELOG-append commit to each. Every commit message is prefixed with
``[skip ci]`` so real CI workflows stay quiet; Lunar code collectors still
fire on push regardless.

The script is intentionally tolerant:
  * One bad repo never aborts the whole run.
  * Activity tier eligibility windows are rough (hourly / ~6h / ~3d); we do
    not require microsecond precision.
  * No AI calls — the whole point is cheap, repeatable load.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from pareto import weighted_choice  # noqa: E402

STATE_DIR = REPO_ROOT / "state"
LEDGER = STATE_DIR / "repos.jsonl"

ORG = os.environ.get("SIMULATOR_ORG", "pantalasa-cronos")
DEFAULT_WEIGHTS = {
    "hot": 60.0,
    "active": 30.0,
    "maintenance": 10.0,
    "dormant": 0.0,
}
DEFAULT_ELIGIBILITY_HOURS = {
    "hot": 1,
    "active": 6,
    "maintenance": 72,
    "dormant": None,  # never eligible
}

MESSAGE_POOL = [
    "bump logs timestamp",
    "note on monitoring cadence",
    "tweak changelog formatting",
    "track hourly snapshot",
    "refresh health-check notes",
    "document retention window",
    "record nightly rollup",
    "adjust ops reminder",
    "rotate status marker",
    "append heartbeat line",
]


@dataclass
class LedgerEntry:
    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def tier(self) -> str:
        return self.raw.get("activity_tier", "dormant")

    @property
    def last_commit_at(self) -> datetime | None:
        v = self.raw.get("last_commit_at") or self.raw.get("created_at")
        if not v:
            return None
        try:
            # Support both "Z" suffix and "+00:00" offsets.
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            return datetime.fromisoformat(v)
        except Exception:
            return None


def log(msg: str) -> None:
    print(f"[activity] {msg}", flush=True)


def load_ledger() -> list[LedgerEntry]:
    if not LEDGER.exists():
        return []
    out: list[LedgerEntry] = []
    with LEDGER.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(LedgerEntry(json.loads(line)))
    return out


def save_ledger(entries: list[LedgerEntry]) -> None:
    tmp = LEDGER.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for e in entries:
            f.write(json.dumps(e.raw, separators=(",", ":")) + "\n")
    tmp.replace(LEDGER)


def eligible(entries: list[LedgerEntry], now: datetime) -> list[LedgerEntry]:
    out: list[LedgerEntry] = []
    for e in entries:
        window = DEFAULT_ELIGIBILITY_HOURS.get(e.tier)
        if window is None:
            continue
        last = e.last_commit_at
        if last is None:
            out.append(e)
            continue
        if now - last >= timedelta(hours=window):
            out.append(e)
    return out


def sample_targets(candidates: list[LedgerEntry], k: int,
                   rng: random.Random) -> list[LedgerEntry]:
    """Sample ``k`` entries (without replacement) weighted by tier."""
    if not candidates:
        return []
    pool = list(candidates)
    rng.shuffle(pool)
    out: list[LedgerEntry] = []
    while pool and len(out) < k:
        pairs = [(e, DEFAULT_WEIGHTS.get(e.tier, 0.0)) for e in pool]
        if sum(w for _, w in pairs) <= 0:
            break
        chosen = weighted_choice(pairs, rng=rng)
        out.append(chosen)
        pool.remove(chosen)
    return out


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def push_commit(entry: LedgerEntry, rng: random.Random) -> bool:
    """Clone, append a line to CHANGELOG.md, commit, push. Returns True on success."""
    with tempfile.TemporaryDirectory(prefix="sim-act-") as tmp:
        workdir = Path(tmp) / entry.name
        try:
            _run(["gh", "repo", "clone", f"{ORG}/{entry.name}", str(workdir), "--", "--depth=1"])
        except Exception as e:
            log(f"clone {entry.name} failed: {e}")
            return False

        changelog = workdir / "CHANGELOG.md"
        msg = rng.choice(MESSAGE_POOL)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        line = f"- {stamp}: {msg}\n"

        try:
            if changelog.exists():
                with changelog.open("a") as f:
                    f.write(line)
            else:
                changelog.write_text(f"# Changelog\n\n{line}")

            commit_msg = f"chore: {msg} [skip ci]"
            for cmd in (
                ["git", "-c", "user.name=cronos-simulator",
                         "-c", "user.email=simulator@pantalasa.org",
                         "add", "CHANGELOG.md"],
                ["git", "-c", "user.name=cronos-simulator",
                         "-c", "user.email=simulator@pantalasa.org",
                         "commit", "-m", commit_msg],
                ["git", "push", "origin", "HEAD"],
            ):
                _run(cmd, cwd=workdir)
        except Exception as e:
            log(f"push {entry.name} failed: {e}")
            return False

    entry.raw["last_commit_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits", type=int, default=50, help="max commits to push this run")
    ap.add_argument("--dry-run", action="store_true", help="log targets without pushing")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    now = datetime.now(timezone.utc)

    entries = load_ledger()
    if not entries:
        log("ledger is empty (no repos yet); nothing to do")
        return 0

    candidates = eligible(entries, now)
    log(f"ledger={len(entries)}, eligible={len(candidates)}, target_commits={args.commits}")

    targets = sample_targets(candidates, args.commits, rng)
    if not targets:
        log("no eligible repos; exiting")
        return 0

    if args.dry_run:
        for t in targets:
            log(f"DRY would push to {ORG}/{t.name} (tier={t.tier})")
        return 0

    succeeded = 0
    for t in targets:
        log(f"pushing to {ORG}/{t.name} (tier={t.tier})")
        if push_commit(t, rng):
            succeeded += 1
        time.sleep(rng.uniform(1.0, 3.0))  # gentle pacing

    save_ledger(entries)
    log(f"done: pushed to {succeeded}/{len(targets)} repos")
    return 0 if succeeded > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
