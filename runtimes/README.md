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
4. Ensure `AGENT_NAME` and `AGENT_RUNTIME` match the assigned worker and its runtime.
