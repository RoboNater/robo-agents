import os
import subprocess
from pathlib import Path


def test_supervisor_reprompts_one_persistent_session_until_release(tmp_path: Path) -> None:
    fake_claude = tmp_path / "fake-claude"
    input_log = tmp_path / "inputs.jsonl"
    telemetry = tmp_path / "worker.jsonl"
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text("{}", encoding="utf-8")
    fake_claude.write_text(
        """#!/usr/bin/env bash
set -eu
count=0
release_event='{"event":"tool_call","phase":"success","outcome": "release"}'
while IFS= read -r line; do
  printf '%s\\n' "$line" >> "$SUPERVISOR_INPUT_LOG"
  count=$((count + 1))
  if [ "$count" -eq 2 ]; then
    printf '%s\\n' "$release_event" >> "$HUB_TELEMETRY_LOG"
  fi
  printf '%s\\n' '{"type":"result","subtype":"success"}'
  if [ "$count" -eq 2 ]; then
    exit 0
  fi
done
""",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts" / "supervise-claude-code.sh"
    env = os.environ | {
        "CLAUDE_BIN": str(fake_claude),
        "CLAUDE_MCP_CONFIG": str(mcp_config),
        "HUB_TELEMETRY_LOG": str(telemetry),
        "CLAUDE_REPROMPT_DELAY_S": "0",
        "CLAUDE_MAX_REPROMPTS": "2",
        "SUPERVISOR_INPUT_LOG": str(input_log),
    }

    completed = subprocess.run(
        [str(script)],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    messages = input_log.read_text(encoding="utf-8").splitlines()
    assert len(messages) == 2
    assert "unattended worker" in messages[0]
    assert "Continue the unattended worker loop" in messages[1]
