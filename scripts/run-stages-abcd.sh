#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  print -u2 "Missing .env. Add ANTHROPIC_API_KEY=\"...\" and GITHUB_TOKEN=\"...\" first."
  exit 1
fi

# Catch the common mistake of pasting a secret as a standalone shell line.
if awk '
  /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
  /^[[:space:]]*(ANTHROPIC_API_KEY|GITHUB_TOKEN|TASKFORGE_REFERENCE_CATALOG)=/ { next }
  { invalid = 1 }
  END { exit invalid ? 0 : 1 }
' .env; then
  print -u2 "Malformed .env: put each value on its assignment line, e.g. ANTHROPIC_API_KEY=\"...\". Secret values were not displayed."
  exit 1
fi

set -a
source .env
set +a

if [[ -z "${ANTHROPIC_API_KEY:-}" || -z "${GITHUB_TOKEN:-}" ]]; then
  print -u2 "Set both ANTHROPIC_API_KEY and GITHUB_TOKEN in .env. Values were not displayed."
  exit 1
fi

exec .venv/bin/python -m taskforge.cli \
  --stage-abcd \
  --mcp-config config/stage-b-mcp.json \
  --seed 7 \
  --count 1 \
  --max-topic-draws 20 \
  --output stage_abcd_runs
