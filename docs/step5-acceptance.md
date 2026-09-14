# Step 5 integrated acceptance demo

The Step 5C scenario uses real GitHub work product and a real interactive Alice
session, with deterministic workers implemented by `scripts/mock-worker.py`.
It is restricted in code to `RoboNater/robo-agents-sandbox` and refuses the
first-merge acceptance run if that repository already contains a merged pull
request. It never deletes a failed run's branch or workspace.

The versioned scenario is
[`scenarios/step5c-untrusted.json`](../scenarios/step5c-untrusted.json). Each
attempt creates a fresh issue, a run-namespaced branch, an additive marker, and
a throwaway PR. Its JSON run manifest records the issue and PR URLs, base and
head SHAs, worker identities, intended disturbances, every scripted action,
and the evidence path. It contains no hub or GitHub credentials.

## Run it

Authenticate `gh` before starting. Use a fresh absolute run directory outside
the repository so its token, hub database, generated MCP configuration, and
Alice runtime state cannot be committed accidentally.

```sh
gh auth status
scripts/prepare-step5-demo.sh /tmp/robo-step5c-001 step5c-001
scripts/launch-step5-alice.sh /tmp/robo-step5c-001
```

The Alice launcher is deliberately interactive, matching spec §8. It installs
the checked-in `alice-orchestrator` skill only inside that run's runtime
directory and starts the hub as Alice's stdio MCP server. It refuses to start
when another listener already owns port 8420; inspect `pgrep -a hub` and stop
only this checkout's listener.

After Alice's session opens, start the scripted workers in a second terminal:

```sh
scripts/launch-step5-workers.sh /tmp/robo-step5c-001
```

Bob reports the seeded PR, Charlie requests the one scripted marker change,
Bob pushes the response commit, and Charlie approves that new head. Alice
still owns every workflow transition, the immediately-before-merge gate,
`gh pr merge --squash --delete-branch --match-head-commit`, release, and
WRAP-UP. The scenario carries instruction-like text in the issue, PR, and an
implementer result; the verifier requires the intended pairing, review round,
durable squash policy, merge, and worker release, so merely reaching a merged
PR is insufficient.

When Alice exits, verify and materialize the evidence:

```sh
scripts/verify-step5-demo.sh /tmp/robo-step5c-001
```

Keep `run.json`, `evidence.json`, Alice's session transcript, and the hub
database together. The GitHub issue, PR, commits, checks, and review comments
are durable remote evidence referenced by those files.

## Crash hooks

The worker launcher accepts one optional deterministic crash point:

```sh
scripts/launch-step5-workers.sh /tmp/robo-step5c-001 after_changes_requested
```

Supported points are `bob:after_check_in`, `charlie:after_check_in`,
`after_initial_result`, `after_changes_requested`, `after_address_result`, and
`after_approval`. A trigger writes its point and timestamp to the manifest and
exits with status 75. These hooks make phase-boundary recovery reproducible;
the complete restart matrix remains Step 8 / issue #31.

## Local validation without GitHub mutation

The scenario driver tests inject a fake `gh` runner. They cover sandbox and
first-merge guards, canonical-text expansion, self-contained Alice prompts,
manifest updates, phase/routing verification, and evidence emission:

```sh
uv run --locked pytest tests/test_mock_worker.py tests/test_step5_launch_scripts.py
```
