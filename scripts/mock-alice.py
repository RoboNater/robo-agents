#!/usr/bin/env python3
"""Mock Alice orchestrator driving one task through a worker (spec §4.3, §7 Step 4).

Usage:
    # 1. Stdio MCP mode: launches hub and runs Alice MCP over stdio (in-process events):
    python scripts/mock-alice.py --mcp --agent bob --harness claude-code
    python scripts/mock-alice.py --mcp --agent charlie --harness codex

    # 2. Database mode: runs against an existing HubStore database.
    # Note: Cross-process database polling only re-evaluates holds at timeout deadlines
    # (spec §2: Alice MCP tools run in-process with the hub). Use lower HUB_DEFAULT_WAIT_S
    # or stdio MCP mode for responsive handoffs.
    python scripts/mock-alice.py --db /path/to/hub.db --agent bob --harness claude-code

    # 3. Step 4B endurance mode against a separately running HTTP hub/worker.
    # Production guardrails require >=3 cycles and >=30 minutes. Point both
    # --telemetry-log here and HUB_TELEMETRY_LOG in the worker at the same file.
    python scripts/mock-alice.py --db /path/to/hub.db --agent bob \
        --harness claude-code --timeout 300 --endurance \
        --telemetry-log /absolute/path/endurance-worker.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import sys
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from agent_hub.database import database, initialize_database
from agent_hub.store import HubStore
from agent_hub_common import (
    AgentStatus,
    ConfigurationError,
    EventKind,
    HubSettings,
    TaskState,
    WorkflowStatus,
)
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Alice] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mock-alice")


def _extract_tool_data(tool_result: Any) -> Any:
    if hasattr(tool_result, "structuredContent") and tool_result.structuredContent is not None:
        return tool_result.structuredContent
    if hasattr(tool_result, "content") and tool_result.content:
        text = tool_result.content[0].text
        try:
            return json.loads(text)
        except Exception:
            return text
    return {}


async def _call(
    session: ClientSession,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    result = await session.call_tool(name, arguments or {})
    if getattr(result, "isError", False):
        error_msg = ""
        if hasattr(result, "content") and result.content:
            error_msg = "; ".join(
                getattr(item, "text", str(item)) for item in result.content
            )
        raise RuntimeError(f"Tool {name} failed: {error_msg or result}")
    return _extract_tool_data(result)


class AliceCrashError(RuntimeError):
    """Raised to simulate Alice crashing."""


ENDURANCE_QUESTION = "May I continue the endurance probe?"


def _part_kind(part: dict[str, Any]) -> Any:
    root = part.get("root")
    normalized = root if isinstance(root, dict) else part
    metadata = normalized.get("metadata")
    return metadata.get("hub.kind") if isinstance(metadata, dict) else None


def _verify_endurance_rows(
    store: HubStore,
    expected_agent: str,
    task_ids: list[str],
    prior_task_ids: set[str],
) -> dict[str, int]:
    """Prove retries did not duplicate durable assignments or mutations."""

    current_ids = {task.id for task in store.tasks() if task.assignee == expected_agent}
    new_ids = current_ids - prior_task_ids
    if new_ids != set(task_ids):
        raise RuntimeError(
            f"Expected exactly {len(task_ids)} new tasks, found {len(new_ids)}: {new_ids}"
        )

    totals = {"assignments": 0, "questions": 0, "replies": 0, "results": 0}
    for cycle, task_id in enumerate(task_ids):
        counts = {key: 0 for key in totals}
        for message in store.task_history(task_id):
            kinds = [_part_kind(part) for part in message.parts if isinstance(part, dict)]
            if message.direction == "from_alice" and "assignment" in kinds:
                counts["assignments"] += 1
            if message.direction == "to_alice" and "question" in kinds:
                counts["questions"] += 1
            if message.direction == "from_alice" and "reply" in kinds:
                counts["replies"] += 1
            if message.direction == "to_alice" and "result" in kinds:
                counts["results"] += 1

        expected = {
            "assignments": 1,
            "questions": 1 if cycle == 0 else 0,
            "replies": 1 if cycle == 0 else 0,
            "results": 1,
        }
        if counts != expected:
            raise RuntimeError(f"Duplicate-row check failed for cycle {cycle + 1}: {counts}")
        for key, count in counts.items():
            totals[key] += count
    return totals


def verify_endurance_telemetry(path: Path, long_task_id: str) -> dict[str, int]:
    """Check the worker log for required timeout retries and timer heartbeats."""

    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read endurance telemetry {path}: {exc}") from exc

    successful = [
        record
        for record in records
        if record.get("event") == "tool_call" and record.get("phase") == "success"
    ]
    assignment_timeouts = sum(
        record.get("tool") == "await_assignment" and record.get("outcome") == "timeout"
        for record in successful
    )
    question_timeouts = sum(
        record.get("tool") == "ask_alice" and record.get("outcome") == "timeout"
        for record in successful
    )
    long_task_heartbeats = sum(
        record.get("event") == "heartbeat"
        and record.get("phase") == "success"
        and record.get("accepted") is True
        and record.get("current_task_id") == long_task_id
        for record in records
    )
    releases = sum(
        record.get("tool") == "await_assignment" and record.get("outcome") == "release"
        for record in successful
    )
    if assignment_timeouts < 1:
        raise RuntimeError("Telemetry has no await_assignment timeout/retry evidence")
    if question_timeouts < 1:
        raise RuntimeError("Telemetry has no ask_alice timeout/retry evidence")
    if long_task_heartbeats < 1:
        raise RuntimeError("Telemetry has no accepted heartbeat during the long work interval")
    if releases < 1:
        raise RuntimeError("Telemetry has no clean release acknowledgement")
    return {
        "assignment_timeouts": assignment_timeouts,
        "question_timeouts": question_timeouts,
        "long_task_heartbeats": long_task_heartbeats,
        "releases": releases,
        "tool_errors": sum(
            record.get("event") == "tool_call" and record.get("phase") == "error"
            for record in records
        ),
        "transport_retries": sum(record.get("event") == "retry" for record in records),
    }


def wait_for_endurance_telemetry(
    path: Path, long_task_id: str, timeout_s: float
) -> dict[str, int]:
    """Wait for the released worker's final telemetry record, then verify it."""

    deadline = monotonic() + timeout_s
    last_error: RuntimeError | None = None
    while monotonic() < deadline:
        try:
            return verify_endurance_telemetry(path, long_task_id)
        except RuntimeError as exc:
            last_error = exc
            sleep(0.5)
    raise last_error or RuntimeError(f"No endurance telemetry appeared at {path}")


async def drive_endurance(
    store: HubStore,
    expected_agent: str,
    *,
    expected_harness: str | None = None,
    cycles: int = 3,
    min_elapsed_s: float = 1800.0,
    assignment_delay_s: float = 45.0,
    cycle_gap_s: float = 480.0,
    worker_hold_s: float = 20.0,
    question_hold_s: float = 20.0,
    question_reply_delay_s: float = 30.0,
    long_work_s: float = 210.0,
    lost_after_s: float = 180.0,
    checkin_timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Drive the Step 4B multi-cycle, long-running worker scenario."""

    if cycles < 3:
        raise ValueError("Endurance scenario requires at least 3 cycles")
    if assignment_delay_s <= worker_hold_s:
        raise ValueError("assignment_delay_s must exceed worker_hold_s")
    if question_reply_delay_s <= question_hold_s:
        raise ValueError("question_reply_delay_s must exceed question_hold_s")
    if long_work_s <= lost_after_s:
        raise ValueError("long_work_s must exceed lost_after_s")

    logger.info("Endurance Alice waiting for worker %r to check in...", expected_agent)
    checkin_deadline = monotonic() + checkin_timeout_s
    agent = store.agent_by_name(expected_agent)
    while (
        agent is None or agent.status == AgentStatus.RELEASED
    ) and monotonic() < checkin_deadline:
        await asyncio.sleep(0.1)
        agent = store.agent_by_name(expected_agent)
    if agent is None or agent.status == AgentStatus.RELEASED:
        raise TimeoutError(f"Worker {expected_agent!r} did not check in")
    if expected_harness is not None and agent.harness != expected_harness:
        raise ValueError(
            f"Worker {expected_agent!r} checked in with harness {agent.harness!r}, "
            f"expected {expected_harness!r}"
        )

    started = monotonic()
    prior_task_ids = {task.id for task in store.tasks() if task.assignee == expected_agent}
    logger.info(
        "Worker checked in; delaying first assignment %.1fs to force an await timeout",
        assignment_delay_s,
    )
    await asyncio.sleep(assignment_delay_s)

    task_ids: list[str] = []
    long_interval_s = 0.0
    heartbeat_before = ""
    heartbeat_after = ""
    last_delivery_id: str | None = None
    per_task_timeout_s = max(300.0, long_work_s + question_reply_delay_s + 120.0)

    for cycle in range(cycles):
        if cycle:
            logger.info(
                "Cycle %d complete; waiting %.1fs before the next assignment", cycle, cycle_gap_s
            )
            await asyncio.sleep(cycle_gap_s)

        if cycle == 0:
            action = (
                f"Call ask_alice for this task with the exact question {ENDURANCE_QUESTION!r} "
                f"and timeout_s={question_hold_s}. When it times out, call ask_alice again "
                "with the identical question until Alice replies."
            )
        elif cycle == 1:
            action = (
                f"Run exactly `sleep {long_work_s}` as one foreground shell command, setting the "
                "shell-tool timeout higher than the sleep duration. Do not background it and do "
                "not use a polling loop. Call no hub MCP tool until it finishes; the worker-mcp "
                "timer must keep you alive independently."
            )
        else:
            action = "Complete this cycle immediately."
        instructions = (
            f"Endurance probe cycle {cycle + 1} of {cycles}. Do not edit repository files. "
            f"{action} Then call submit_result once with an ImplementerResult whose outcome is "
            "completed, summary names this cycle, pr_url is "
            "https://github.com/RoboNater/robo-agents/pull/30, and head_sha is "
            "0123456789abcdef0123456789abcdef01234567. After completion, keep looping on "
            f"await_assignment(timeout_s={worker_hold_s}); retry every timeout until release."
        )
        task = store.assign_task(
            expected_agent,
            "implementer",
            f"Endurance cycle {cycle + 1}",
            instructions,
            lease_min=max(30.0, (long_work_s + 300.0) / 60.0),
        )
        task_ids.append(task.id)
        assigned_at = monotonic()
        if cycle == 1:
            current = store.agent_by_name(expected_agent)
            heartbeat_before = current.last_heartbeat if current is not None else ""
        logger.info("Assigned endurance cycle %d: %s", cycle + 1, task.id)

        replied = False
        task_deadline = monotonic() + per_task_timeout_s
        finished = False
        while monotonic() < task_deadline:
            event = await store.wait_for_event(timeout_s=0.5, ack=last_delivery_id)
            last_delivery_id = None
            if event is None:
                continue
            last_delivery_id = event.delivery_id
            if event.kind == EventKind.WORKER_QUESTION and event.payload.get("task_id") == task.id:
                if cycle != 0:
                    raise RuntimeError(f"Unexpected question during cycle {cycle + 1}")
                if not replied:
                    logger.info(
                        "Holding question reply %.1fs to force ask_alice timeout/retry",
                        question_reply_delay_s,
                    )
                    await asyncio.sleep(question_reply_delay_s)
                    applied = store.reply(
                        task.id,
                        "Approved. Continue the endurance probe.",
                        message_id=event.payload.get("message_id"),
                    )
                    if not applied:
                        raise RuntimeError("Endurance question reply was not applied")
                    replied = True
            elif (
                event.kind in (EventKind.TASK_COMPLETED, EventKind.TASK_FAILED)
                and event.payload.get("task_id") == task.id
            ):
                if event.kind is EventKind.TASK_FAILED:
                    raise RuntimeError(f"Endurance cycle {cycle + 1} failed: {event.payload}")
                finished = True
                break
        if not finished:
            raise TimeoutError(f"Endurance cycle {cycle + 1} did not finish")
        if cycle == 0 and not replied:
            raise RuntimeError("Worker completed cycle 1 without the required question")
        if cycle == 1:
            long_interval_s = monotonic() - assigned_at
            current = store.agent_by_name(expected_agent)
            heartbeat_after = current.last_heartbeat if current is not None else ""
            if long_interval_s < long_work_s:
                raise RuntimeError(
                    f"Long work interval lasted {long_interval_s:.1f}s, "
                    f"expected >= {long_work_s:.1f}s"
                )
            if not heartbeat_before or heartbeat_after <= heartbeat_before:
                raise RuntimeError("Worker heartbeat did not advance during the long work interval")

    remaining = min_elapsed_s - (monotonic() - started)
    if remaining > 0:
        logger.info("Holding release %.1fs so the run reaches its minimum duration", remaining)
        await asyncio.sleep(remaining)
    if last_delivery_id:
        store.ack_event(last_delivery_id)

    elapsed_s = monotonic() - started
    store.release_agent(expected_agent)
    store.set_workflow_status(
        WorkflowStatus.DONE,
        f"Endurance scenario completed {cycles} cycles in {elapsed_s:.1f}s",
    )
    row_counts = _verify_endurance_rows(store, expected_agent, task_ids, prior_task_ids)
    logger.info("Endurance scenario complete; database duplicate checks passed: %s", row_counts)
    return {
        "agent": expected_agent,
        "harness": agent.harness,
        "elapsed_s": round(elapsed_s, 3),
        "cycles": cycles,
        "task_ids": task_ids,
        "long_work_interval_s": round(long_interval_s, 3),
        "heartbeat_advanced": heartbeat_after > heartbeat_before,
        "row_counts": row_counts,
    }


async def drive_one_task_mcp(
    session: ClientSession,
    expected_agent: str,
    role: str = "implementer",
    title: str = "Fix issue #1",
    instructions: str = "Implement the requested changes and add tests.",
    timeout_s: float = 60.0,
    expected_harness: str | None = None,
    crash_at: str | None = None,
) -> dict[str, Any]:
    """Drive one worker using Alice's MCP tools over stdio."""
    logger.info("Alice (MCP) is ready. Waiting for worker %r to check in...", expected_agent)

    deadline = asyncio.get_running_loop().time() + timeout_s
    agent_name: str | None = None
    checked_in_harness: str | None = None
    last_delivery_id: str | None = None

    active_states = (
        TaskState.WORKING.value,
        TaskState.SUBMITTED.value,
        TaskState.INPUT_REQUIRED.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
    )
    existing_task = None
    state_data = await _call(session, "get_state", {})
    state_tasks = state_data.get("tasks") if isinstance(state_data, dict) else []
    for t in state_tasks or []:
        if (
            isinstance(t, dict)
            and t.get("assignee") == expected_agent
            and t.get("state") in active_states
        ):
            existing_task = t
            break

    task_finished = False
    result_data: dict[str, Any] = {}

    if existing_task:
        agent_name = expected_agent
        task_id = existing_task.get("id") or ""
        logger.info(
            "Worker %r already has task %s (state=%s), resuming...",
            agent_name,
            task_id,
            existing_task.get("state"),
        )
    else:
        checkin_event_id: Any = None
        while not agent_name and asyncio.get_running_loop().time() < deadline:
            if crash_at == "before_ack" and last_delivery_id:
                raise AliceCrashError("Simulated Alice crash before ack")

            state_data = await _call(session, "get_state", {})
            known_ag = next(
                (
                    ag
                    for ag in (state_data.get("agents") or [])
                    if isinstance(ag, dict)
                    and ag.get("name") == expected_agent
                    and ag.get("status") != AgentStatus.RELEASED.value
                ),
                None,
            )
            timeout_to_use = (
                0.05
                if (known_ag is not None and not last_delivery_id)
                else max(0.05, min(2.0, deadline - asyncio.get_running_loop().time()))
            )
            wait_args: dict[str, Any] = {"timeout_s": timeout_to_use}
            if last_delivery_id:
                wait_args["ack"] = last_delivery_id
            data = await _call(session, "wait_for_event", wait_args)
            event = data.get("event") if isinstance(data, dict) else None
            if event is None:
                if known_ag is not None:
                    agent_name = expected_agent
                    checked_in_harness = known_ag.get("harness")
                    break
                continue

            last_delivery_id = event.get("delivery_id")
            if crash_at == "delivery":
                raise AliceCrashError("Simulated Alice crash at delivery")

            if (
                isinstance(event, dict)
                and event.get("kind") == "agent_checked_in"
                and (event.get("payload") or {}).get("agent") == expected_agent
            ):
                agent_name = expected_agent
                payload = event.get("payload") or {}
                checked_in_harness = payload.get("harness")
                checkin_event_id = event.get("id")
                break

        if not agent_name:
            raise TimeoutError(f"Worker {expected_agent!r} did not check in within {timeout_s}s")

        if expected_harness is not None:
            if checked_in_harness != expected_harness:
                raise ValueError(
                    f"Worker {agent_name!r} checked in with harness {checked_in_harness!r}, "
                    f"expected {expected_harness!r}"
                )
            logger.info("Worker %r harness verified: %s", agent_name, checked_in_harness)

        logger.info("Worker %r checked in! Assigning task...", agent_name)
        checkpoint_key = f"event:{checkin_event_id}:assign"
        await _call(
            session,
            "log_decision",
            {
                "summary": f"Assigned task to {agent_name} for role {role}",
                "rationale": f"Initial assignment for role {role}",
                "key": checkpoint_key,
            },
        )
        assign_data = await _call(
            session,
            "assign_task",
            {"agent": agent_name, "role": role, "title": title, "instructions": instructions},
        )
        task_id = assign_data.get("id") if isinstance(assign_data, dict) else ""
        logger.info("Task assigned: id=%s title=%r", task_id, title)

        if crash_at == "after_action":
            raise AliceCrashError("Simulated Alice crash after action")

    while not task_finished and asyncio.get_running_loop().time() < deadline:
        if crash_at == "before_ack" and last_delivery_id:
            raise AliceCrashError("Simulated Alice crash before ack")

        wait_args = {"timeout_s": 5.0}
        if last_delivery_id:
            wait_args["ack"] = last_delivery_id
        data = await _call(session, "wait_for_event", wait_args)
        event = data.get("event") if isinstance(data, dict) else None
        if not event or not isinstance(event, dict):
            if existing_task and existing_task.get("state") in ("completed", "failed"):
                task_finished = True
                result_data = existing_task.get("result") or {}
                break
            continue

        last_delivery_id = event.get("delivery_id")
        if crash_at == "delivery":
            raise AliceCrashError("Simulated Alice crash at delivery")

        kind = event.get("kind")
        payload = event.get("payload") or {}
        logger.info("Observed event: %s", kind)

        if kind == "agent_checked_in":
            continue
        elif kind == "task_progress":
            logger.info("Progress reported: %s", payload.get("note"))
        elif kind == "worker_question":
            q_task_id = payload.get("task_id")
            q_msg_id = payload.get("message_id")
            logger.info("Worker asked question on %s: %r", q_task_id, payload.get("question"))
            await _call(
                session,
                "reply",
                {
                    "task_id": q_task_id,
                    "text": "Approved. Proceed with the proposed design.",
                    "message_id": q_msg_id,
                },
            )
            logger.info("Alice replied to question on %s", q_task_id)
            if crash_at in ("after_action", "after_reply"):
                raise AliceCrashError("Simulated Alice crash after reply")
        elif (
            kind in ("task_completed", "task_failed")
            and payload.get("task_id") == task_id
        ):
            task_finished = True
            result_data = payload
            logger.info(
                "Task %s reached terminal state: %s (summary=%r)",
                task_id,
                kind,
                payload.get("summary"),
            )
            result_payload = payload.get("result")
            if isinstance(result_payload, dict):
                logger.info(
                    "Typed result: outcome=%s verdict=%s pr_url=%s head_sha=%s",
                    result_payload.get("outcome"),
                    result_payload.get("verdict"),
                    result_payload.get("pr_url"),
                    result_payload.get("head_sha") or result_payload.get("reviewed_head_sha"),
                )

    if not task_finished:
        raise TimeoutError(f"Task {task_id} did not finish within {timeout_s}s")

    if last_delivery_id:
        await _call(session, "wait_for_event", {"timeout_s": 0.05, "ack": last_delivery_id})

    logger.info("Releasing worker %r...", agent_name)
    await _call(session, "release_agent", {"agent": agent_name})
    await _call(
        session,
        "set_workflow_status",
        {"status": "done", "summary": f"Task {task_id} finished successfully"},
    )
    logger.info("Workflow marked DONE. Mock Alice session complete.")
    return result_data


async def drive_one_task(
    store: HubStore,
    expected_agent: str,
    role: str = "implementer",
    title: str = "Fix issue #1",
    instructions: str = "Implement the requested changes and add tests.",
    timeout_s: float = 60.0,
    expected_harness: str | None = None,
    crash_at: str | None = None,
) -> dict[str, Any]:
    """Drive one worker through the full lifecycle using HubStore directly:

    check_in -> assignment -> progress -> question & reply -> completed -> release
    """
    logger.info("Alice is ready. Waiting for worker %r to check in...", expected_agent)

    # 1. Wait for agent check-in
    deadline = asyncio.get_running_loop().time() + timeout_s
    agent_name: str | None = None
    checked_in_harness: str | None = None
    last_delivery_id: str | None = None

    active_states = (
        TaskState.WORKING.value,
        TaskState.SUBMITTED.value,
        TaskState.INPUT_REQUIRED.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
    )
    existing_task = None
    for t in store.get_state()["tasks"]:
        if t["assignee"] == expected_agent and t["state"] in active_states:
            existing_task = t
            break

    task_finished = False
    result_data: dict[str, Any] = {}

    if existing_task:
        agent_name = expected_agent
        task_record = store.get_task(existing_task["id"])
        assert task_record is not None
        task = task_record
        logger.info(
            "Worker %r already has task %s (state=%s), resuming...",
            agent_name,
            task.id,
            existing_task["state"],
        )
    else:
        checkin_event_id: int | None = None
        while asyncio.get_running_loop().time() < deadline:
            if crash_at == "before_ack" and last_delivery_id:
                raise AliceCrashError("Simulated Alice crash before ack")

            known_agent = store.agent_by_name(expected_agent)
            timeout_to_use = (
                0.05
                if known_agent is not None and known_agent.status != AgentStatus.RELEASED
                else max(0.05, min(2.0, deadline - asyncio.get_running_loop().time()))
            )
            event = await store.wait_for_event(timeout_s=timeout_to_use, ack=last_delivery_id)
            if event is None:
                if known_agent is not None and known_agent.status != AgentStatus.RELEASED:
                    agent_name = known_agent.name
                    checked_in_harness = known_agent.harness
                    break
                continue
            last_delivery_id = event.delivery_id
            if crash_at == "delivery":
                raise AliceCrashError("Simulated Alice crash at delivery")

            matched = (
                event.kind == EventKind.AGENT_CHECKED_IN
                and event.payload.get("agent") == expected_agent
            )
            if matched:
                agent_name = expected_agent
                checked_in_harness = event.payload.get("harness")
                checkin_event_id = event.id
                break

        if not agent_name:
            raise TimeoutError(f"Worker {expected_agent!r} did not check in within {timeout_s}s")

        if expected_harness is not None:
            if checked_in_harness != expected_harness:
                raise ValueError(
                    f"Worker {agent_name!r} checked in with harness {checked_in_harness!r}, "
                    f"expected {expected_harness!r}"
                )
            logger.info("Worker %r harness verified: %s", agent_name, checked_in_harness)

        logger.info("Worker %r checked in! Assigning task...", agent_name)
        if checkin_event_id is None:
            with database(store.path) as connection:
                query = (
                    "SELECT id FROM event WHERE kind = ? AND "
                    "json_extract(payload_json, '$.agent') = ? "
                    "ORDER BY id DESC LIMIT 1"
                )
                row = connection.execute(
                    query, (EventKind.AGENT_CHECKED_IN.value, agent_name)
                ).fetchone()
                if row:
                    checkin_event_id = row["id"]
        checkpoint_key = f"event:{checkin_event_id}:assign"
        store.log_decision(
            f"Assigned task to {agent_name} for role {role}",
            f"Initial assignment for role {role}",
            key=checkpoint_key,
        )
        # 2. Assign task
        task = store.assign_task(
            agent=agent_name,
            role=role,
            title=title,
            instructions=instructions,
            lease_min=30,
        )
        logger.info("Task assigned: id=%s title=%r", task.id, title)

        if crash_at == "after_action":
            raise AliceCrashError("Simulated Alice crash after action")

    # 3. Wait for progress / question / completion
    while not task_finished and asyncio.get_running_loop().time() < deadline:
        if crash_at == "before_ack" and last_delivery_id:
            raise AliceCrashError("Simulated Alice crash before ack")

        event = await store.wait_for_event(timeout_s=5.0, ack=last_delivery_id)
        if event is None:
            if existing_task and existing_task.get("state") in ("completed", "failed"):
                task_finished = True
                result_data = existing_task.get("result") or {}
                break
            continue
        last_delivery_id = event.delivery_id
        if crash_at == "delivery":
            raise AliceCrashError("Simulated Alice crash at delivery")

        logger.info("Observed event: %s", event.kind)

        if event.kind == EventKind.AGENT_CHECKED_IN:
            continue
        elif event.kind == EventKind.TASK_PROGRESS:
            logger.info("Progress reported by worker: %s", event.payload.get("note"))

        elif event.kind == EventKind.WORKER_QUESTION:
            q_task_id = event.payload.get("task_id")
            question = event.payload.get("question")
            q_msg_id = event.payload.get("message_id")
            logger.info("Worker asked question on %s: %r", q_task_id, question)
            store.reply(
                q_task_id,
                "Approved. Proceed with the proposed design.",
                message_id=q_msg_id,
            )
            logger.info("Alice replied to question on %s", q_task_id)
            if crash_at in ("after_action", "after_reply"):
                raise AliceCrashError("Simulated Alice crash after reply")

        elif event.kind in (EventKind.TASK_COMPLETED, EventKind.TASK_FAILED):
            completed_id = event.payload.get("task_id")
            if completed_id == task.id:
                task_finished = True
                result_data = event.payload
                logger.info(
                    "Task %s reached terminal state: %s (summary=%r)",
                    completed_id,
                    event.kind,
                    event.payload.get("summary"),
                )
                result_payload = event.payload.get("result")
                if isinstance(result_payload, dict):
                    logger.info(
                        "Typed result: outcome=%s verdict=%s pr_url=%s head_sha=%s",
                        result_payload.get("outcome"),
                        result_payload.get("verdict"),
                        result_payload.get("pr_url"),
                        result_payload.get("head_sha") or result_payload.get("reviewed_head_sha"),
                    )
                if crash_at == "after_action":
                    raise AliceCrashError("Simulated Alice crash after action")

    if not task_finished:
        raise TimeoutError(f"Task {task.id} did not finish within {timeout_s}s")

    if last_delivery_id:
        store.ack_event(last_delivery_id)

    # 4. Release worker
    logger.info("Releasing worker %r...", agent_name)
    store.release_agent(agent_name)
    store.set_workflow_status(WorkflowStatus.DONE, f"Task {task.id} finished successfully")
    logger.info("Workflow marked DONE. Mock Alice session complete.")

    return result_data


def _parse_cmd(cmd: str) -> list[str]:
    """Parse a shell command string into arguments, supporting Windows paths with spaces."""
    if sys.platform == "win32":
        return [part.strip("\"'") for part in shlex.split(cmd, posix=False)]
    return shlex.split(cmd)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Alice orchestrator")
    try:
        default_db = str(HubSettings.from_env().database_path)
    except ConfigurationError:
        default_db = "hub.db"
    parser.add_argument(
        "--db",
        default=default_db,
        help=(
            "Path to SQLite database file (applies to direct DB mode; "
            "--mcp manages state via HUB_STATE_DIR in the environment)"
        ),
    )
    parser.add_argument("--agent", default="bob", help="Expected worker agent name")
    parser.add_argument("--role", default="implementer", help="Task role to assign")
    parser.add_argument(
        "--harness",
        "--runtime",
        dest="harness",
        default="claude-code",
        help="Expected worker harness (claude-code, codex, etc.); --runtime is the Step 4 name",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="Hold timeout in seconds")
    parser.add_argument(
        "--crash-at",
        choices=["delivery", "after_action", "before_ack", "after_reply"],
        default=None,
        help="Simulate crash point for Alice",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Drive Alice over MCP stdio by launching the hub server process",
    )
    parser.add_argument(
        "--hub-cmd",
        default=f'"{sys.executable}" -m agent_hub.main',
        help="Command to launch hub when running with --mcp (parsed quote-aware)",
    )
    parser.add_argument(
        "--endurance",
        action="store_true",
        help="Run the Step 4B multi-cycle scenario (direct database mode only)",
    )
    parser.add_argument("--cycles", type=int, default=3, help="Endurance assignment cycles")
    parser.add_argument(
        "--min-elapsed-s", type=float, default=1800.0, help="Minimum endurance run duration"
    )
    parser.add_argument(
        "--assignment-delay-s",
        type=float,
        default=45.0,
        help="Delay before cycle 1; must exceed the worker hold timeout",
    )
    parser.add_argument(
        "--cycle-gap-s", type=float, default=480.0, help="Delay between completed cycles"
    )
    parser.add_argument(
        "--worker-hold-s", type=float, default=20.0, help="Hold requested by worker instructions"
    )
    parser.add_argument(
        "--question-hold-s", type=float, default=20.0, help="Question hold in cycle 1"
    )
    parser.add_argument(
        "--question-reply-delay-s",
        type=float,
        default=30.0,
        help="Alice reply delay; must exceed the question hold",
    )
    parser.add_argument(
        "--long-work-s",
        type=float,
        default=None,
        help="No-tool work interval; defaults to HUB_LOST_AFTER_S + 30",
    )
    parser.add_argument(
        "--telemetry-log",
        type=Path,
        default=None,
        help="Worker JSONL telemetry to verify after an endurance run",
    )

    args = parser.parse_args()

    try:
        if args.endurance:
            if args.mcp:
                parser.error("--endurance cannot be combined with --mcp")
            if args.cycles < 3:
                parser.error("--cycles must be at least 3")
            if args.min_elapsed_s < 1800:
                parser.error("--min-elapsed-s must be at least 1800 for an endurance run")
            if args.telemetry_log is None:
                parser.error("--telemetry-log is required for an endurance run")
            if not args.telemetry_log.is_absolute():
                parser.error("--telemetry-log must be an absolute path")
            db_path = Path(args.db).resolve()
            initialize_database(db_path)
            store = HubStore(db_path)
            lost_after_s = HubSettings.from_env().lost_after_s
            long_work_s = (
                lost_after_s + 30.0 if args.long_work_s is None else args.long_work_s
            )
            result = asyncio.run(
                drive_endurance(
                    store,
                    args.agent,
                    expected_harness=args.harness,
                    cycles=args.cycles,
                    min_elapsed_s=args.min_elapsed_s,
                    assignment_delay_s=args.assignment_delay_s,
                    cycle_gap_s=args.cycle_gap_s,
                    worker_hold_s=args.worker_hold_s,
                    question_hold_s=args.question_hold_s,
                    question_reply_delay_s=args.question_reply_delay_s,
                    long_work_s=long_work_s,
                    lost_after_s=lost_after_s,
                    checkin_timeout_s=args.timeout,
                )
            )
            if args.telemetry_log is not None:
                telemetry = wait_for_endurance_telemetry(
                    args.telemetry_log.resolve(),
                    result["task_ids"][1],
                    args.worker_hold_s + 30.0,
                )
                result["telemetry"] = telemetry
        elif args.mcp:
            cmd_parts = _parse_cmd(args.hub_cmd)
            params = StdioServerParameters(
                command=cmd_parts[0],
                args=cmd_parts[1:],
                env=dict(os.environ),
            )

            async def run_mcp_session() -> dict[str, Any]:
                async with (
                    stdio_client(params) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    return await drive_one_task_mcp(
                        session=session,
                        expected_agent=args.agent,
                        role=args.role,
                        timeout_s=args.timeout,
                        expected_harness=args.harness,
                        crash_at=args.crash_at,
                    )

            result = asyncio.run(run_mcp_session())
        else:
            db_path = Path(args.db).resolve()
            initialize_database(db_path)
            store = HubStore(db_path)
            result = asyncio.run(
                drive_one_task(
                    store=store,
                    expected_agent=args.agent,
                    role=args.role,
                    timeout_s=args.timeout,
                    expected_harness=args.harness,
                    crash_at=args.crash_at,
                )
            )
        print(f"SUCCESS: {result}")
    except Exception as exc:
        logger.exception("Mock Alice failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
