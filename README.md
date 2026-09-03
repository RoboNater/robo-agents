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
creates `hub.db` and a mode-`0600` bearer token file under
`$XDG_STATE_HOME/agent-hub` (falling back to `~/.local/state/agent-hub`), so the
same state is found again no matter which working directory the process is
started from; set `HUB_STATE_DIR` (which must be absolute) to move it. A
relative `XDG_STATE_HOME` is invalid per the XDG base-directory specification
and is ignored in favour of the `~/.local/state` fallback. Override the other
defaults with the variables documented in [`.env.example`](.env.example). For a
deployed process, inject `HUB_TOKEN` rather than sharing the generated token
file.

Step 1 only provisions the token and advertises the `bearerAuth` scheme on the
agent card; **nothing is enforced yet.** Bearer enforcement on the protected
routes is a Step 2 deliverable (see §4.1 and §7 of the spec), so treat the hub as
unauthenticated until then and keep it bound to loopback.

`HUB_PUBLIC_URL` is the address the agent card advertises, not the bind address.
It defaults to `http://HUB_HOST:HUB_PORT`, which is correct only for a loopback
bind; binding the unspecified address in any spelling (`0.0.0.0`, `::`,
`0:0:0:0:0:0:0:0`, `*`) to reach remote workers requires setting
`HUB_PUBLIC_URL` to a dialable address such as `http://alice-host:8420`.

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
