#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 || $1 != /* ]]; then
  echo "usage: $0 ABSOLUTE_RUN_DIR" >&2; exit 2
fi
run_dir=$1
readarray -t settings < <(python3 - "$run_dir/run.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
assert m['repository'] == 'RoboNater/robo-agents-sandbox' and m.get('issue')
print(m['models']['alice'])
print(m['alice_session_id'])
PY
)
if curl --silent --fail http://127.0.0.1:8420/healthz >/dev/null 2>&1; then
  echo 'port 8420 occupied; leave other checkout listeners alone' >&2; exit 1
fi
cd "$run_dir/alice-runtime"
exec claude --model "${settings[0]}" --session-id "${settings[1]}" \
  --strict-mcp-config --mcp-config "$run_dir/alice.mcp.json" \
  --permission-mode acceptEdits --allowedTools 'Bash(gh *),mcp__hub__*' \
  -- "$(<"$run_dir/alice.prompt.md")"
