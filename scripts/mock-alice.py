#!/usr/bin/env python3
"""Mock Alice orchestrator driving one task through a worker (spec §4.3, §7 Step 4).

Usage:
    # 1. Stdio MCP mode: launches hub and runs Alice MCP over stdio (in-process events):
    python scripts/mock-alice.py --mcp --agent bob --runtime claude-code
    python scripts/mock-alice.py --mcp --agent charlie --runtime codex

    # 2. Database mode: runs against an existing HubStore database.
    # Note: Cross-process database polling only re-evaluates holds at timeout deadlines
    # (spec §2: Alice MCP tools run in-process with the hub). Use lower HUB_DEFAULT_WAIT_S
    # or stdio MCP mode for responsive handoffs.
    python scripts/mock-alice.py --db /path/to/hub.db --agent bob --runtime claude-code
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path
from typing import Any

from agent_hub.database import database, initialize_database
from agent_hub.store import HubStore
from agent_hub_common import EventKind, HubSettings, WorkflowStatus
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


async def drive_one_task_mcp(
    session: ClientSession,
    expected_agent: str,
    role: str = "implementer",
    title: str = "Fix issue #1",
    instructions: str = "Implement the requested changes and add tests.",
    timeout_s: float = 60.0,
    expected_runtime: str | None = None,
) -> dict[str, Any]:
    """Drive one worker using Alice's MCP tools over stdio."""
    logger.info("Alice (MCP) is ready. Waiting for worker %r to check in...", expected_agent)

    deadline = asyncio.get_running_loop().time() + timeout_s
    agent_name: str | None = None

    while asyncio.get_running_loop().time() < deadline:
        res = await session.call_tool("wait_for_event", {"timeout_s": 2.0})
        data = _extract_tool_data(res)
        event = data.get("event") if isinstance(data, dict) else None

        if event is None:
            state_res = await session.call_tool("get_state", {})
            state_data = _extract_tool_data(state_res)
            agents = state_data.get("agents") if isinstance(state_data, dict) else []
            for ag in agents or []:
                if isinstance(ag, dict) and ag.get("name") == expected_agent:
                    agent_name = expected_agent
                    if expected_runtime and ag.get("runtime") != expected_runtime:
                        raise ValueError(
                            f"Worker {agent_name!r} checked in with runtime "
                            f"{ag.get('runtime')!r}, expected {expected_runtime!r}"
                        )
                    break
            if agent_name:
                break
            continue

        if (
            isinstance(event, dict)
            and event.get("kind") == "agent_checked_in"
            and (event.get("payload") or {}).get("agent") == expected_agent
        ):
            agent_name = expected_agent
            payload = event.get("payload") or {}
            runtime = payload.get("runtime")
            if expected_runtime and runtime and runtime != expected_runtime:
                raise ValueError(
                    f"Worker {agent_name!r} checked in with runtime {runtime!r}, "
                    f"expected {expected_runtime!r}"
                )
            break

    if not agent_name:
        raise TimeoutError(f"Worker {expected_agent!r} did not check in within {timeout_s}s")

    logger.info("Worker %r checked in! Assigning task...", agent_name)

    assign_res = await session.call_tool(
        "assign_task",
        {"agent": agent_name, "role": role, "title": title, "instructions": instructions},
    )
    assign_data = _extract_tool_data(assign_res)
    task_id = assign_data.get("id") if isinstance(assign_data, dict) else ""
    logger.info("Task assigned: id=%s title=%r", task_id, title)

    await session.call_tool(
        "log_decision",
        {
            "decision": f"Assigned task {task_id} to {agent_name}",
            "rationale": f"Initial assignment for role {role}",
        },
    )

    task_finished = False
    result_data: dict[str, Any] = {}

    while not task_finished and asyncio.get_running_loop().time() < deadline:
        res = await session.call_tool("wait_for_event", {"timeout_s": 5.0})
        data = _extract_tool_data(res)
        event = data.get("event") if isinstance(data, dict) else None
        if not event or not isinstance(event, dict):
            continue

        kind = event.get("kind")
        payload = event.get("payload") or {}
        logger.info("Observed event: %s", kind)

        if kind == "task_progress":
            logger.info("Progress reported: %s", payload.get("note"))
        elif kind == "worker_question":
            q_task_id = payload.get("task_id")
            logger.info("Worker asked question on %s: %r", q_task_id, payload.get("question"))
            await session.call_tool(
                "reply",
                {"task_id": q_task_id, "text": "Approved. Proceed with the proposed design."},
            )
            logger.info("Alice replied to question on %s", q_task_id)
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

    if not task_finished:
        raise TimeoutError(f"Task {task_id} did not finish within {timeout_s}s")

    logger.info("Releasing worker %r...", agent_name)
    await session.call_tool("release_agent", {"agent": agent_name})
    await session.call_tool(
        "set_workflow_status",
        {"status": "done", "summary": f"Task {task_id} finished successfully"},
    )
    logger.info("Workflow marked DONE. Mock Alice session complete.")
    return result_data


def _agent_runtime(store: HubStore, agent_name: str) -> str | None:
    with database(store.path) as connection:
        rows = connection.execute(
            "SELECT payload FROM event WHERE kind = ? ORDER BY id DESC",
            (EventKind.AGENT_CHECKED_IN.value,),
        ).fetchall()
        for r in rows:
            p = json.loads(r["payload"])
            if p.get("agent") == agent_name:
                return p.get("runtime")
    return None


async def drive_one_task(
    store: HubStore,
    expected_agent: str,
    role: str = "implementer",
    title: str = "Fix issue #1",
    instructions: str = "Implement the requested changes and add tests.",
    timeout_s: float = 60.0,
    expected_runtime: str | None = None,
) -> dict[str, Any]:
    """Drive one worker through the full lifecycle using HubStore directly:

    check_in -> assignment -> progress -> question & reply -> completed -> release
    """
    logger.info("Alice is ready. Waiting for worker %r to check in...", expected_agent)

    # 1. Wait for agent check-in
    deadline = asyncio.get_running_loop().time() + timeout_s
    agent_name: str | None = None
    checked_in_runtime: str | None = None

    while asyncio.get_running_loop().time() < deadline:
        event = await store.wait_for_event(timeout_s=2.0)
        if event is None:
            # Check if agent already checked in before Alice waited
            agent = store.agent_by_name(expected_agent)
            if agent is not None:
                agent_name = agent.name
                checked_in_runtime = _agent_runtime(store, agent_name)
                break
            continue
        matched = (
            event.kind == EventKind.AGENT_CHECKED_IN
            and event.payload.get("agent") == expected_agent
        )
        if matched:
            agent_name = expected_agent
            checked_in_runtime = event.payload.get("runtime")
            break

    if not agent_name:
        raise TimeoutError(f"Worker {expected_agent!r} did not check in within {timeout_s}s")

    if expected_runtime is not None:
        runtime = checked_in_runtime or _agent_runtime(store, agent_name)
        if runtime != expected_runtime:
            raise ValueError(
                f"Worker {agent_name!r} checked in with runtime {runtime!r}, "
                f"expected {expected_runtime!r}"
            )
        logger.info("Worker %r runtime verified: %s", agent_name, runtime)

    logger.info("Worker %r checked in! Assigning task...", agent_name)

    # 2. Assign task
    task = store.assign_task(
        agent=agent_name,
        role=role,
        title=title,
        instructions=instructions,
        lease_min=30,
    )
    logger.info("Task assigned: id=%s title=%r", task.id, title)
    store.log_decision(
        f"Assigned task {task.id} to {agent_name}",
        f"Initial assignment for role {role}",
    )

    # 3. Wait for progress / question / completion
    task_finished = False
    result_data: dict[str, Any] = {}

    while not task_finished and asyncio.get_running_loop().time() < deadline:
        event = await store.wait_for_event(timeout_s=5.0)
        if event is None:
            continue
        logger.info("Observed event: %s", event.kind)

        if event.kind == EventKind.TASK_PROGRESS:
            logger.info("Progress reported by worker: %s", event.payload.get("note"))

        elif event.kind == EventKind.WORKER_QUESTION:
            q_task_id = event.payload.get("task_id")
            question = event.payload.get("question")
            logger.info("Worker asked question on %s: %r", q_task_id, question)
            store.reply(q_task_id, "Approved. Proceed with the proposed design.")
            logger.info("Alice replied to question on %s", q_task_id)

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

    if not task_finished:
        raise TimeoutError(f"Task {task.id} did not finish within {timeout_s}s")

    # 4. Release worker
    logger.info("Releasing worker %r...", agent_name)
    store.release_agent(agent_name)
    store.set_workflow_status(WorkflowStatus.DONE, f"Task {task.id} finished successfully")
    logger.info("Workflow marked DONE. Mock Alice session complete.")

    return result_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Alice orchestrator")
    try:
        default_db = str(HubSettings.from_env().db_path)
    except Exception:
        default_db = "hub.db"
    parser.add_argument(
        "--db",
        default=default_db,
        help="Path to SQLite database file (defaults to HUB_DB_PATH from environment)",
    )
    parser.add_argument("--agent", default="bob", help="Expected worker agent name")
    parser.add_argument("--role", default="implementer", help="Task role to assign")
    parser.add_argument(
        "--runtime",
        default="claude-code",
        help="Worker runtime (claude-code, codex, etc.)",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="Hold timeout in seconds")
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Drive Alice over MCP stdio by launching the hub server process",
    )
    parser.add_argument(
        "--hub-cmd",
        default=f"{sys.executable} -m agent_hub.main",
        help="Command to launch hub when running with --mcp",
    )

    args = parser.parse_args()

    try:
        if args.mcp:
            cmd_parts = shlex.split(args.hub_cmd)
            params = StdioServerParameters(command=cmd_parts[0], args=cmd_parts[1:])

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
                        expected_runtime=args.runtime,
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
                    expected_runtime=args.runtime,
                )
            )
        print(f"SUCCESS: {result}")
    except Exception as exc:
        logger.exception("Mock Alice failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
