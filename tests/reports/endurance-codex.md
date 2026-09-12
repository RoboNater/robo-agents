# Codex endurance report

- Date: 2026-09-12
- Result: PASS
- Harness/version: Codex CLI 0.154.0
- Provider/model: OpenAI / `gpt-5.6-sol`
- Worker profile: `charlie`, heartbeat interval 30 seconds
- Harness elapsed time: 1,800.126 seconds
- Completed assignment cycles: 3

## Observations

The timed `scripts/mock-alice.py --endurance` scenario completed without a
human message after the measured worker session started. The worker completed
all three tasks, returned to `await_assignment` after each result, and consumed
Alice's final release response.

- `await_assignment` returned `timeout` 56 times, including the deliberately
  delayed first assignment, and the worker retried.
- `ask_alice` returned `timeout` once; the worker repeated the identical
  question and consumed Alice's reply.
- Cycle 2 contained a foreground `sleep 210`. Alice measured 247.73 seconds
  between assignment and result, exceeding `HUB_LOST_AFTER_S=180`.
- Eight accepted heartbeat records occurred while cycle 2 was active, and the
  heartbeat timestamp advanced throughout the long-work interval.
- The exact database counts were 3 assignments, 1 question, 1 reply, and 3
  results. The harness found no duplicate rows.
- Telemetry recorded one tool error: Codex probed the not-yet-authored Step 5
  implementer role guide and received HTTP 404. It was unrelated to the
  endurance workflow. Transport retry count was zero.
- The worker MCP call timeout was configured above the harness timeout. No MCP
  transport timeout was observed; the explicit 20-second hub await and question
  timeouts were observed as successful tool outcomes.
- No context compaction was observed.
- No supervisor was used. An invalid Codex approval flag was corrected before
  the measured session; the measured session itself required no intervention.

## Reproduction

The run used an isolated hub with `HUB_LOST_AFTER_S=180`, a 30-second worker
heartbeat, and an absolute `HUB_TELEMETRY_LOG`. Alice was run as:

```sh
uv run --locked python scripts/mock-alice.py \
  --db /tmp/issue30-codex/hub.db \
  --agent charlie --harness codex --timeout 300 \
  --endurance --telemetry-log /tmp/issue30-codex/worker.jsonl
```

The final harness summary was:

```text
elapsed_s=1800.126 cycles=3 long_work_interval_s=247.73
assignment_timeouts=56 question_timeouts=1 long_task_heartbeats=8
tool_errors=1 transport_retries=0
row_counts={assignments: 3, questions: 1, replies: 1, results: 3}
```
