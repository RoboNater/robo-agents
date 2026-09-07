#!/usr/bin/env python3
"""Mock Alice orchestrator driving one task through a worker (spec §4.3, §7 Step 4).

Usage:
    # Run against a local HubStore/DB driving a worker:
    python scripts/mock-alice.py --agent bob --runtime claude-code
    python scripts/mock-alice.py --agent charlie --runtime codex
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from agent_hub.database import initialize_database
from agent_hub.store import HubStore
from agent_hub_common import EventKind, WorkflowStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Alice] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mock-alice")


async def drive_one_task(
    store: HubStore,
    expected_agent: str,
    role: str = "implementer",
    title: str = "Fix issue #1",
    instructions: str = "Implement the requested changes and add tests.",
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Drive one worker through the full lifecycle:

    check_in -> assignment -> progress -> question & reply -> completed -> release
    """
    logger.info("Alice is ready. Waiting for worker %r to check in...", expected_agent)

    # 1. Wait for agent check-in
    deadline = asyncio.get_running_loop().time() + timeout_s
    agent_name: str | None = None
    while asyncio.get_running_loop().time() < deadline:
        event = await store.wait_for_event(timeout_s=2.0)
        if event is None:
            # Check if agent already checked in before Alice waited
            agent = store.agent_by_name(expected_agent)
            if agent is not None:
                agent_name = agent.name
                break
            continue
        matched = (
            event.kind == EventKind.AGENT_CHECKED_IN
            and event.payload.get("agent") == expected_agent
        )
        if matched:
            agent_name = expected_agent
            break

    if not agent_name:
        raise TimeoutError(f"Worker {expected_agent!r} did not check in within {timeout_s}s")

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
    parser.add_argument("--db", default="hub.db", help="Path to SQLite database file")
    parser.add_argument("--agent", default="bob", help="Expected worker agent name")
    parser.add_argument("--role", default="implementer", help="Task role to assign")
    parser.add_argument(
        "--runtime",
        default="claude-code",
        help="Worker runtime (claude-code, codex, etc.)",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="Hold timeout in seconds")

    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    initialize_database(db_path)
    store = HubStore(db_path)

    try:
        result = asyncio.run(
            drive_one_task(
                store=store,
                expected_agent=args.agent,
                role=args.role,
                timeout_s=args.timeout,
            )
        )
        print(f"SUCCESS: {result}")
    except Exception as exc:
        logger.exception("Mock Alice failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
