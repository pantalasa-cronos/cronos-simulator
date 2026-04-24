# cronos-simulator

AI-driven load-test generator for the `pantalasa-cronos` demo org and the
`cronos.demo.earthly.dev` Lunar hub.

Two cron-driven workflows:

- **Generate repos** — creates ~38 AI-authored repos per run, 4x per day
  (configurable) until `TARGET_REPOS` (default 1000) is reached.
- **Simulate activity** — pushes Pareto-weighted `[skip ci]` commits across
  the generated fleet every hour to exercise code collectors without
  saturating CI runners.

See the full plan at
[earthly-agent-config/plans/cronos-load-test-implementation.md](https://github.com/brandonSc/earthly-agent-config/blob/main/plans/cronos-load-test-implementation.md).

## Layout

```
cronos-simulator/
├── .github/workflows/
│   ├── seed-company.yml         manual: regenerate seed/company.json
│   ├── generate-repos.yml       cron 4x/day + workflow_dispatch
│   └── simulate-activity.yml    cron hourly + workflow_dispatch
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

## Repository variables (Settings → Secrets and variables → Actions → Variables)

All are **optional**. Defaults in parens.

| Name | Default | Purpose |
|------|---------|---------|
| `ENABLED` | — | `true` to enable scheduled `generate-repos` runs. Anything else means scheduled runs no-op. `workflow_dispatch` with `force=true` always runs. |
| `ACTIVITY_ENABLED` | — | Same kill-switch for `simulate-activity`. |
| `SIMULATOR_ORG` | `pantalasa-cronos` | GitHub org to target. |
| `TARGET_REPOS` | `1000` | `gen-repo.py` self-stops once the ledger has this many entries (unless `--force`). |
| `REPOS_PER_RUN` | `38` | How many repos each scheduled `generate-repos` run creates. |
| `COMMITS_PER_RUN` | `50` | Max commits per `simulate-activity` run. |
| `SLEEP_MIN_SECONDS` / `SLEEP_MAX_SECONDS` | `30` / `60` | Sleep window between repo creations. |
| `CLAUDE_MODEL` | `sonnet` | Model for `gen-repo.py` (`opus`, `sonnet`, or full model id). |
| `CLAUDE_MAX_BUDGET_USD` | `0.50` | Per-invocation soft cap passed to `claude --max-budget-usd`. |

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

Edit the single `cron:` line in the workflow. Common alternatives are noted in-line. Preferable to leave `cron` on the default and use the repo-var kill switch for short-term pauses.

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

- Every simulated commit message ends with `[skip ci]` so the demo's self-hosted runners stay quiet. Lunar code collectors still fire on push.
- The `gen-repo.py` script tolerates per-repo failures and sleeps between repos to stay under GitHub & Anthropic rate limits.
- No real user data is generated; all emails/names end with `@pantalasa.org`.
