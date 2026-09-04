# Agent Comms Hub

This repository implements the proof of concept described in
[`docs/poc-spec.md`](docs/poc-spec.md). The current implementation covers plan
Steps 1–2: the uv workspace, shared configuration and bearer-token provisioning,
the SQLite schema, A2A agent-card discovery, and the hub core — the A2A request
handlers workers speak, Alice's event queue, the lease and heartbeat sweeper,
and bearer enforcement on the protected routes.

## Run the hub

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

`HUB_PUBLIC_URL` is the address the agent card advertises, not the bind address.
It defaults to `http://HUB_HOST:HUB_PORT`, which is correct only for a loopback
bind; binding the unspecified address in any spelling (`0.0.0.0`, `::`,
`0:0:0:0:0:0:0:0`, `*`) to reach remote workers requires setting
`HUB_PUBLIC_URL` to a dialable address such as `http://alice-host:8420`.

To load a local `.env` file explicitly:

```sh
uv run --env-file .env hub
```

## What is public and what is not

Discovery and health are the entire public surface — a worker needs the card
before it holds a token, and health checks run before any credential exists:

```sh
curl http://127.0.0.1:8420/.well-known/agent-card.json
curl http://127.0.0.1:8420/healthz
```

Everything else requires the pre-shared token. `POST /a2a` answers `401` with a
`WWW-Authenticate: Bearer` challenge when the header is missing, malformed, or
carries a token that does not match:

```sh
TOKEN=$(cat ~/.local/state/agent-hub/token)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8420/a2a \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tasks/get","params":{"id":"x"}}'
# 401
```

## Talking to the hub

Workers are A2A clients; the hub is the only server. Check in with `READY` to
get the `contextId` every later call uses:

```sh
curl -s -X POST http://127.0.0.1:8420/a2a \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{
        "messageId":"m1","role":"user","parts":[{"kind":"text","text":"READY"}],
        "metadata":{"agent":"bob","capabilities":["python"],"runtime":"claude-code"}}}}'
```

Then poll for work with `NEXT` on `message/stream`. The hub holds the response
open until Alice assigns a task or releases the agent, and returns a
`metadata.timeout` marker at the deadline so no agent ever spins:

```sh
curl -sN -X POST http://127.0.0.1:8420/a2a \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"message/stream","params":{"message":{
        "messageId":"m2","role":"user","parts":[{"kind":"text","text":"NEXT"}],
        "contextId":"<contextId from the check-in>"}}}'
```

Progress notes, questions and results are `message/send` and `message/stream`
calls carrying a `taskId` and a `metadata.kind` of `progress`, `question` or
`result` — see §4.1 of the spec for the full mapping. A question that times out
must be retried under the `messageId` it was first asked with (the timeout
marker echoes it as `metadata.retry_as_message_id`), so that a reply Alice sent
between the two attempts still reaches the worker. Assignments themselves
come from Alice, whose MCP tools land in Step 3 and run inside this same
process; until then nothing assigns work, so a `NEXT` will hold to its deadline.

## Checks

```sh
uv sync --locked --all-packages --dev
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
```

Alice's MCP tools and the worker MCP tools intentionally remain unimplemented
until plan Steps 3–4.
