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
#   CLAUDE_MODEL=claude-opus-4-5 ./scripts/gen-company.sh   # write to seed/company.json
#   DRY_RUN=1                    ./scripts/gen-company.sh   # print to stdout only
#
# Required env:
#   ANTHROPIC_API_KEY   (used as x-api-key for direct Anthropic API calls)
#
# We call the Anthropic API directly via curl rather than going through the
# Claude Code CLI. Claude Code has its own OAuth layer that does not read
# ANTHROPIC_API_KEY the same way and was returning "Invalid API key" in CI.
# The direct REST API only needs the env var and is plenty for a JSON-output
# one-shot prompt.
set -euo pipefail

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ANTHROPIC_API_KEY is not set" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${REPO_ROOT}/seed/company.json"

# CLAUDE_MODEL may be an alias ("opus", "sonnet") for the Claude Code CLI or
# a full model id for the REST API. Default to a full id here.
MODEL="${CLAUDE_MODEL:-claude-opus-4-5}"
case "$MODEL" in
    opus)   MODEL="claude-opus-4-5" ;;
    sonnet) MODEL="claude-sonnet-4-5" ;;
    haiku)  MODEL="claude-haiku-4-5" ;;
esac

MAX_TOKENS="${MAX_TOKENS:-16000}"

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

echo "Calling Anthropic API (model=$MODEL, max_tokens=$MAX_TOKENS) ..." >&2

REQ_BODY=$(MODEL="$MODEL" MAX_TOKENS="$MAX_TOKENS" python3 -c '
import json, sys, os
prompt = sys.stdin.read()
body = {
    "model": os.environ["MODEL"],
    "max_tokens": int(os.environ["MAX_TOKENS"]),
    "messages": [{"role": "user", "content": prompt}],
}
print(json.dumps(body))
' <<<"$PROMPT")

RESP=$(curl -sS -w '\n__HTTP_STATUS__%{http_code}' \
    https://api.anthropic.com/v1/messages \
    -H "x-api-key: $ANTHROPIC_API_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "content-type: application/json" \
    --data-binary "$REQ_BODY")

STATUS=$(printf '%s' "$RESP" | sed -n 's/.*__HTTP_STATUS__\([0-9]*\)$/\1/p')
BODY=$(printf '%s' "$RESP" | sed 's/\n__HTTP_STATUS__[0-9]*$//')

echo "HTTP $STATUS" >&2

if [ "$STATUS" != "200" ]; then
    echo "Anthropic API error:" >&2
    echo "$BODY" >&2
    exit 1
fi

JSON=$(echo "$BODY" | python3 -c '
import json, re, sys
resp = json.load(sys.stdin)
text = "".join(b["text"] for b in resp["content"] if b.get("type") == "text")
text = text.strip()
text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
text = re.sub(r"\n?```$", "", text)
start = text.find("{")
if start == -1:
    sys.stderr.write("No JSON object in response text\n")
    sys.exit(1)
depth, end = 0, None
for i, ch in enumerate(text[start:], start=start):
    if ch == "{":
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0:
            end = i + 1
            break
if end is None:
    sys.stderr.write("Unbalanced JSON in response text\n")
    sys.exit(1)
obj = json.loads(text[start:end])
print(json.dumps(obj, indent=2))
')

if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "$JSON"
    exit 0
fi

echo "$JSON" > "$OUT"
echo "Wrote $OUT" >&2

python3 -c "
import json
with open('$OUT') as f: d = json.load(f)
print('domains:', len(d.get('domains', {})))
print('subdomains:', sum(len(v.get('children', [])) for v in d.get('domains', {}).values()))
print('people:', len(d.get('people', [])))
print('commit_authors:', len(d.get('commit_authors', [])))
" >&2
