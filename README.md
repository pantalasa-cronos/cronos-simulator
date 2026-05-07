# cronos-simulator

AI-driven load-test generator for the `pantalasa-cronos` demo org and the
`cronos.demo.earthly.dev` Lunar hub.

Two cron-driven workflows:

- **Generate repos** — creates ~38 AI-authored repos per run, 4x per day
  (configurable) until `TARGET_REPOS` (default 1000) is reached.
- **Simulate activity** — pushes Pareto-weighted commits across the generated
  fleet on a **15-minute** schedule so load is steadier than a single hourly
  burst. A configurable fraction (`CI_SAMPLE_PCT`, default 50%) trigger the
  component repo's CI workflow (and the lunar-ci-action inside it); the rest
  carry `[skip ci]` to keep Actions spend bounded. Batch size **scales with
  fleet size** (line count of `state/repos.jsonl`, i.e. simulated repos the
  hub catalog sees from this org).

See the full plan at
[earthly-agent-config/plans/cronos-load-test-implementation.md](https://github.com/brandonSc/earthly-agent-config/blob/main/plans/cronos-load-test-implementation.md).

## Layout

```
cronos-simulator/
├── .github/workflows/
│   ├── seed-company.yml         manual: regenerate seed/company.json
│   ├── generate-repos.yml       cron 4x/day + workflow_dispatch
│   └── simulate-activity.yml    cron every 15m + workflow_dispatch
├── seed/
│   ├── company.json             AI-generated persona (10 domains × 2-4 subs)
│   └── archetypes.yml           weighted languages × roles × lifecycles
├── scripts/
│   ├── gen-company.sh           Claude prompt → seed/company.json
│   ├── gen-repo.py              creates ONE or N repos per invocation
│   ├── simulate-activity.py     Pareto-weighted commit pusher
│   └── pareto.py                shared weighting helper
└── state/
    └── repos.jsonl              append-only ledger (committed by CI)
```

## Secrets (Settings → Secrets and variables → Actions → Secrets)

| Name | Purpose |
|------|---------|
| `GH_PAT_CRONOS_SIMULATOR` | Classic PAT with `repo` + `admin:org` scopes; used to create/archive repos in `pantalasa-cronos` and to commit the ledger. |
| `ANTHROPIC_API_KEY_CRONOS_SIMULATOR` | Dedicated Anthropic key (separate from bender's) so spend is isolated. Only needed for `seed-company` and `generate-repos`. |
| `LUNAR_HUB_TOKEN` | Cronos hub token. `gen-repo.py` forwards this onto every newly-created repo as a repo-level Actions secret so each repo's `ci.yml` lunar-ci-action step can authenticate to `cronos.demo.earthly.dev`. Use `scripts/sweep-repo-secrets.py` to set it on repos that already exist. |

## Repository variables (Settings → Secrets and variables → Actions → Variables)

All are **optional**. Defaults in parens.

| Name | Default | Purpose |
|------|---------|---------|
| `ENABLED` | — | `true` to enable scheduled `generate-repos` runs. Anything else means scheduled runs no-op. `workflow_dispatch` with `force=true` always runs. |
| `ACTIVITY_ENABLED` | — | Same kill-switch for `simulate-activity`. |
| `SIMULATOR_ORG` | `pantalasa-cronos` | GitHub org to target. |
| `TARGET_REPOS` | `1000` | `gen-repo.py` self-stops once the ledger has this many entries (unless `--force`). |
| `REPOS_PER_RUN` | `38` | How many repos each scheduled `generate-repos` run creates. |
| `COMMITS_SCALE_PCT` | `2` | Integer percent of ledger lines per run: `commits = lines × pct ÷ 100`, then clamped to `COMMITS_MIN`..`COMMITS_PER_RUN`. |
| `COMMITS_MIN` | `5` | Floor after scaling (small fleet still gets steady activity). |
| `COMMITS_PER_RUN` | `80` | Ceiling after scaling. For **Simulate activity** manual runs: workflow input **Commits** = `0` (default) uses this scaling; a positive number fixes the batch size for that run only. |
| `SLEEP_MIN_SECONDS` / `SLEEP_MAX_SECONDS` | `30` / `60` | Sleep window between repo creations. |
| `CLAUDE_MODEL` | `sonnet` | Model for `gen-repo.py` (`opus`, `sonnet`, or full model id). |
| `CLAUDE_MAX_BUDGET_USD` | `0.50` | Per-invocation soft cap passed to `claude --max-budget-usd`. |
| `CI_SAMPLE_PCT` | `50` | 0–100. Percentage of simulated commits that should **trigger CI** (omit `[skip ci]`) so the lunar-ci-action runs and reports to the hub. `0` = all commits skip CI (cheapest); `100` = every commit triggers CI. The **Simulate activity** workflow_dispatch also exposes a per-run override input. |

## Usage

### First-time setup

1. Create this repo as **`pantalasa-cronos/cronos-simulator`** (private).
2. Add both secrets above.
3. Run the **Seed company** workflow manually (Actions → Seed company → Run workflow). It opens a PR with `seed/company.json`; review and merge.
4. Set vars: `ENABLED=false`, `ACTIVITY_ENABLED=false` while you smoke-test.
5. Run **Generate repos** manually with `count=1, force=true` to confirm the pipeline end-to-end.
6. Verify both catalogers (`github-org`, `backstage-cronos`) pick up the new repo on the cronos hub (see the Grafana SQL pointers in the workspace `AGENTS.md`).
7. Set `ENABLED=true` and `ACTIVITY_ENABLED=true` to start the ramp.

### Manual +150 after 1000 is reached

Actions → **Generate repos** → Run workflow → set `count=150`, `force=true`.

### Kill switch

Set `ENABLED=false` (or `ACTIVITY_ENABLED=false`). Scheduled runs become no-ops on the next tick.

### Changing cadence

Edit the `cron:` line in `simulate-activity.yml` (default: every 15 minutes). Prefer the kill switch `ACTIVITY_ENABLED=false` for short pauses instead of removing the schedule.

**Rough daily volume (auto mode):** 96 runs/day at 15 minutes × `clamp(lines × pct / 100, min, max)` commits per run (e.g. 500 lines, 2%, min 5, max 80 → 10 commits/run → ~960 commits/day). Raise `COMMITS_PER_RUN` or lower the cron interval only if runners and hub capacity allow it.

## How the ledger works

`state/repos.jsonl` is an append-only newline-delimited JSON file. Each line looks like:

```json
{"name":"payments-ledger-api","archetype":{"language":"go","role":"api-service","lifecycle":"production"},"activity_tier":"active","domain":"payments.ledger","owner":"jane@pantalasa.org","created_at":"2026-04-24T10:15:00Z","last_commit_at":""}
```

- `generate-repos` appends a line per repo successfully created and commits the file.
- `simulate-activity` updates `last_commit_at` in-place and commits the file.
- Runs serialize on a `concurrency` group per workflow, so the ledger never sees parallel writers.

If the ledger is ever corrupted, both scripts fail loudly rather than silently duplicate repos.

## Catalog population

- **github-org cataloger** (hourly, lives in `earthly/lunar-lib`) — gives us every repo as a component plus its visibility + GitHub topics.
- **backstage-cronos cataloger** (hourly, lives in `pantalasa-cronos/lunar/catalogers/backstage-cronos`) — reads each repo's `catalog-info.yaml` and layers on `owner` + `domain`.

The cronos-simulator writes both `lunar.yml` and `catalog-info.yaml` into every repo so the catalogers have something to work with.

## Notes

- A configurable fraction of simulated commits (`CI_SAMPLE_PCT`, default 50%) trigger the component repo's CI workflow on push, which runs the lunar-ci-action and reports to the hub; the rest carry `[skip ci]`. Lunar code collectors fire on push regardless of the suffix.
- The `gen-repo.py` script tolerates per-repo failures and sleeps between repos to stay under GitHub & Anthropic rate limits.
- No real user data is generated; all emails/names end with `@pantalasa.org`.
