# Agent Comms Hub

This repository implements the proof of concept described in
[`docs/poc-spec.md`](docs/poc-spec.md). The current implementation covers plan
Step 1: the uv workspace, shared configuration and bearer-token provisioning,
the SQLite schema, and A2A agent-card discovery.

## Run the scaffold

```sh
uv run hub
```

The hub listens on `http://127.0.0.1:8420` by default. On first startup it
creates `.hub/hub.db` and a mode-`0600` bearer token at `.hub/token`. Override
the defaults with the variables documented in [`.env.example`](.env.example).
For a deployed process, inject `HUB_TOKEN` rather than sharing the generated
token file.

To load a local `.env` file explicitly:

```sh
uv run --env-file .env hub
```

Discovery and health endpoints are public:

```sh
curl http://127.0.0.1:8420/.well-known/agent-card.json
curl http://127.0.0.1:8420/healthz
```

Run the Step 1 checks with:

```sh
uv run pytest
uv run ruff check .
uv run mypy packages tests
```

The A2A request handlers, Alice MCP tools, and worker MCP tools intentionally
remain unimplemented until plan Steps 2–4.
