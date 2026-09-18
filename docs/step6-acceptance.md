# Step 6 localhost acceptance

The versioned scenario is `scenarios/step6-localhost-untrusted.json`. It requires
interactive Claude Alice, supervised Claude Bob, and unsupervised Codex Charlie.
The control session launches these independent runtimes; Alice only assigns
through the hub. The driver may write the declared head canary and unrelated
base PR, and may never review or merge the work PR for an agent.

Use persistent storage outside this checkout for the measured run. Generated
configs contain the bearer token. Tokens, raw transcripts, the SQLite database,
authentication symlink, and all three clones stay in that private run directory.
Never commit them. Authentication and first-use workspace trust must be settled
before measurement; do not use permission-bypass flags.

Before consuming a sandbox issue, run:

```sh
uv sync --locked --all-packages --dev
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
```

The tests execute bootstrap and preparation with disposable local repositories,
all launchers with fake runtimes, and the driver/verifier entry points. Shell
scripts must also pass `bash -n`. Read every edited function in its final form.

Pin exact models using `STEP6_ALICE_MODEL`, `STEP6_BOB_MODEL`, and
`STEP6_CHARLIE_MODEL` before preparation. Defaults are the previously proven
`claude-sonnet-5` and `gpt-5.6-sol`. Actual CLI versions and model IDs are recorded
in the manifest and generated configurations. Verify the configured models with
harmless CLI calls before seeding. `--approve-for-me` already selects
workspace-write in Codex 0.154.0 and cannot be combined with `--sandbox`.
Its run-local config allows network access for GitHub and hub HTTP; the six
worker MCP tools are approved individually. Bob's supervisor supplies a fixed
continuation prompt and makes no workflow decisions.

```sh
pgrep -a hub
claude --version
claude auth status
codex --version
codex login status
gh repo view RoboNater/robo-agents-sandbox --json viewerPermission,squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed
gh workflow list --repo RoboNater/robo-agents-sandbox
scripts/prepare-step6-demo.sh /absolute/persistent/step6-attempt --seed
```

Preparation refuses an existing run directory, creates independent full clones
and owner-only identity files, checks cross-clone uncommitted isolation, renders
trusted prompts, then creates a fresh issue last. It records the issue atomically
and adds an identified reservation comment to coordination roadmap #2 without
marking Step 6 complete. Setup failures are preserved; no clone is force-reset
or deleted. A new measured attempt uses a fresh directory and namespace.

Start these in separate long-lived PTYs and capture output beneath the run root:

```sh
scripts/launch-step6-alice.sh /absolute/persistent/step6-attempt
scripts/launch-step6-bob.sh /absolute/persistent/step6-attempt > /absolute/persistent/step6-attempt/bob.transcript.jsonl 2> /absolute/persistent/step6-attempt/bob.stderr
scripts/launch-step6-charlie.sh /absolute/persistent/step6-attempt > /absolute/persistent/step6-attempt/charlie.transcript.jsonl 2> /absolute/persistent/step6-attempt/charlie.stderr
scripts/run-step6-disturbances.py /absolute/persistent/step6-attempt > /absolute/persistent/step6-attempt/driver.stdout 2> /absolute/persistent/step6-attempt/driver.stderr
```

Alice launches the hub through her stdio MCP server. Wait for `/healthz`, inspect
`get_state`, and verify the exact durable goal/policy before launching workers.
Confirm both profiles and distinct workspace IDs before she pairs them. Answer
only authentication/trust UI before measurement; never reprompt workers to do
workflow work. Bob's transport supervisor may repeat its fixed continuation.

The first draft normalizer strips whitespace but does not fold case. Charlie
inspects/tests its exact SHA and records `changes_requested`, finding `r1-1`
with tag `STEP6-NORMALIZE-001`. Bob's ADDRESS adds casefold and a regression test.
Charlie approves that new head. The driver waits for that exact approval and CI,
pushes one run-specific head canary, then waits for Charlie's approval at the
moved head. Alice records the actual head-mismatch gate and assigns RE-REVIEW.
The driver then opens and CI-checks an additive unrelated PR and squash-merges
it with a head match. Alice records the actual stale-base gate, assigns Bob
REBASE, waits for exact-head CI, and alone performs the work PR's SHA-bound
squash merge. All state/checkpoints and failed attempts remain intact.

Alice's trusted prompt requests actual gate JSON under `step6:gate:head`,
`step6:gate:base`, and `step6:gate:final` decision keys. These are observations,
not asserted expected results. It also requests a post-merge CLOSE-OUT task,
follow-up references or a verified empty list, and release decisions after
WRAP-UP. The driver never writes these audit records.

After done and both workers have observed release:

```sh
scripts/verify-step6-demo.sh /absolute/persistent/step6-attempt
```

The verifier reads a consistent SQLite snapshot, durable GitHub issue/PR/comment,
commit and exact-SHA check-run facts, and executed tool calls from transcripts.
It retrieves Alice's session JSONL from the standard Claude projects directory
using the session ID recorded before launch. Preserve that source or copy it to
`alice.transcript.jsonl` before verification if using a different Claude config
root. The verifier emits private `github-facts.json`, `hub-audit.json`,
`tool-audit.json`, and `evidence.json`, and exits unsuccessfully on any absent or
contradictory requirement. Workspace-access conclusions concern recorded tool
calls; the shared-account/full-clone arrangement is a guide boundary, not an OS
filesystem or GitHub identity separation. Do not infer success from a canary
being present alone.

Commit only explicitly redacted manifest/evidence/audit extracts and a narrative
under `docs/evidence/`. Include exact reproduction commands, runtime versions,
all issue/PR/commit URLs, retries and deviations. Disclose unavailable private
branch protection and the residual base-movement race. Do not change repository
visibility or account tier as part of a run.

If a rail escalates, diagnose without impersonating an agent. Do not manually
push Bob's fixes, review for Charlie, or merge for Alice. Stop a failed measured
attempt, preserve it, fix the cause, and start a fresh namespace. Driver restart
uses its checkpoints and GitHub facts and refuses ambiguous movement.

Open the coordination PR with `Closes #28` and, only after successful real-worker
injection proof, `Closes #29`. Wait for CI and independent exact-head review.
Changes to behavior, launch policy, driver sequencing, or verifier requirements
normally require a fresh live proof. Phase 7 squash merge and roadmap completion
are separate authorized actions: mark Step 6 complete only after the coordination
PR merges, citing its SHA and sandbox evidence; leave Steps 7/8 and the branch
protection choice open. Stop the hub listener and CLI children belonging to this
run, leaving other checkout listeners and all workspaces intact.
