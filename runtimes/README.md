# Worker Runtime Configurations

This directory contains template configurations for worker agent runtimes (Claude Code, OpenAI Codex CLI, Gemini CLI) to connect to the agent hub via `worker-mcp`.

## Template Files

- `claude-code.mcp.json` — For Claude Code (e.g., Bob).
- `codex.config.toml` — For Codex CLI (e.g., Charlie).
- `gemini.settings.json` — For Gemini CLI.

## Platform Setup & Absolute Paths

All templates use `uv run --directory /path/to/agent-hub worker-mcp` so worker runtimes can launch the MCP server from any working directory or sandbox repo.

Before using a template:
1. Replace `/path/to/agent-hub` with the absolute path to the `agent-hub` workspace checkout:
   - **Linux / macOS**: `/home/user/path/to/robo-agents`
   - **Windows**: Use a Windows absolute path, either with forward slashes (e.g., `C:/work/robo-agents`) or escaped backslashes (e.g., `C:\work\robo-agents`).
2. Replace `http://alice-host:8420` with your hub's public address (e.g., `http://127.0.0.1:8420` for local runs).
3. Replace `HUB_TOKEN` with the shared bearer token (from `$HUB_STATE_DIR/token` or `.env`).
4. Ensure `AGENT_NAME` names the assigned worker.
5. Fill in the identity profile (spec §3), which Alice's role policy pairs workers on:

   | Variable | Meaning | Template value |
   |---|---|---|
   | `HUB_HARNESS` | Agent harness running the worker | `claude-code` / `codex` / `gemini` |
   | `HUB_HARNESS_VERSION` | Harness version, e.g. from `claude --version` | empty |
   | `HUB_PROVIDER` | Model provider | `anthropic` / `openai` / `google` |
   | `HUB_MODEL` | Exact model ID, when the launcher pins one | empty |
   | `HUB_CAPABILITIES` | Comma-separated capabilities matched against `implementer_capabilities` / `reviewer_capabilities` | empty |

   An empty or unset variable is reported as `unknown` (capabilities as none), never guessed.
   Change the provider if the harness is pointed elsewhere (e.g. Claude Code on Bedrock).
   When `HUB_MODEL` is empty the agent may declare its own model through `check_in(model=...)`,
   recorded with `model_source: declared`; a value set here wins and is recorded as `env`.
   `AGENT_RUNTIME`, the Step 4 name for `HUB_HARNESS`, is still honoured when `HUB_HARNESS` is unset.

For endurance runs, set `HUB_TELEMETRY_LOG` to an absolute path. `worker-mcp`
appends JSON Lines records for MCP tool calls and outcomes, errors, HTTP retry
attempts, and timer heartbeats. Reusing the path across a supervised restart is
intentional: each process has a distinct `session_id` and `worker_instance_id`.
