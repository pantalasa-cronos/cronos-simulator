#!/usr/bin/env bash
#
# One-off company persona generator.
# Writes seed/company.json containing domains, subdomains, owners, and people.
#
# Runs via the manual "Seed company" GitHub Actions workflow. Regenerate only
# if we want a new persona — the file is committed so every subsequent
# repo-generation run sees the same corporate shape.
#
# Usage:
#   CLAUDE_MODEL=opus ./scripts/gen-company.sh          # write to seed/company.json
#   DRY_RUN=1        ./scripts/gen-company.sh           # print to stdout only
#
# Required env:
#   ANTHROPIC_API_KEY   (Claude Code reads this)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${REPO_ROOT}/seed/company.json"
MODEL="${CLAUDE_MODEL:-opus}"
BUDGET="${CLAUDE_MAX_BUDGET_USD:-1.00}"

mkdir -p "$(dirname "$OUT")"

PROMPT=$(cat <<'PROMPT_EOF'
You are generating a fictional company "persona" JSON for a load-test tool
that simulates 1000+ repositories. Produce EXACTLY one JSON object (no prose,
no fences) with this shape:

{
  "company": {
    "name": "Pantalasa",
    "tagline": "<one sentence>",
    "summary": "<2-3 sentence company summary>"
  },
  "domains": {
    "<top-level-slug>": {
      "owner": "<lead-email>@pantalasa.org",
      "description": "<short description>",
      "children": ["<subdomain-slug>", ...]
    },
    ...
  },
  "people": [
    {"email": "<first.last>@pantalasa.org",
     "name":  "<First Last>",
     "title": "<job title>",
     "domains": ["<top>.<sub>", "<top>.<sub>"]},
    ...
  ],
  "commit_authors": [
    {"name": "<First Last>", "email": "<first.last>@pantalasa.org"},
    ...
  ]
}

Constraints:
  - EXACTLY 10 top-level domains. Suggested set (use this or a comparable mix):
    platform, product, data, security, infra, payments, growth, ml, mobile, tooling.
  - Each top-level domain has 2-4 children (aim for roughly 30 total subdomains).
  - Slugs are kebab-case, lowercase, no spaces.
  - Domain+subdomain strings in people[].domains use dot notation, e.g. "platform.api-gateway".
  - Generate 30-50 people; distribute them across domains so most subdomains have
    at least 1-2 people assigned.
  - All emails end with @pantalasa.org.
  - commit_authors is a list of 10-15 plausible contributors re-used by the
    activity simulator for commit attribution.
  - Return ONLY the JSON object. No markdown, no explanation.
PROMPT_EOF
)

echo "Invoking claude (model=$MODEL) ..." >&2
OUTPUT=$(claude \
  -p "$PROMPT" \
  --model "$MODEL" \
  --output-format text \
  --dangerously-skip-permissions \
  --max-budget-usd "$BUDGET" 2>/dev/null)

# Trim to the first balanced JSON object.
JSON=$(echo "$OUTPUT" | python3 -c '
import json, sys, re
raw = sys.stdin.read().strip()
# Strip any markdown fences that sneak in.
raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
raw = re.sub(r"\n?```$", "", raw)
start = raw.find("{")
if start == -1:
    sys.stderr.write("No JSON object found in claude output\n")
    sys.exit(1)
depth = 0
end = None
for i, ch in enumerate(raw[start:], start=start):
    if ch == "{":
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0:
            end = i + 1
            break
if end is None:
    sys.stderr.write("Unbalanced JSON in claude output\n")
    sys.exit(1)
obj = json.loads(raw[start:end])
print(json.dumps(obj, indent=2))
')

if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "$JSON"
    exit 0
fi

echo "$JSON" > "$OUT"
echo "Wrote $OUT" >&2

python3 -c "
import json, sys
with open('$OUT') as f: d = json.load(f)
print('domains:', len(d.get('domains', {})))
print('subdomains:', sum(len(v.get('children', [])) for v in d.get('domains', {}).values()))
print('people:', len(d.get('people', [])))
print('commit_authors:', len(d.get('commit_authors', [])))
" >&2
