#!/usr/bin/env python3
"""Mock worker CLI script coordinating with Alice through the hub (spec §4.3, §4.4).

Usage:
    python scripts/mock-worker.py --agent bob --runtime claude-code
    python scripts/mock-worker.py --agent charlie --runtime codex
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from agent_hub_common import (
    AgentProfile,
    ConfigurationError,
    HubSettings,
    ImplementerOutcome,
    ImplementerResult,
    ReviewerResult,
    ReviewerVerdict,
)
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Worker] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mock-worker")


async def run_worker(
    settings: WorkerSettings,
    *,
    http_client: Any | None = None,
    ask_question: str | None = None,
    fail: bool = False,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Execute the full worker coordination lifecycle:

    check_in -> await_assignment -> get_role_guide -> work -> submit_result -> release
    """
    async with WorkerHubClient(settings, http_client=http_client) as client:
        logger.info(
            "Worker %r (%s) checking in at %s...",
            settings.agent_name,
            settings.profile.harness,
            settings.hub_url,
        )
        checkin_res = await client.check_in(["python", "testing"])
        logger.info("Checked in successfully: context_id=%s", checkin_res.get("context_id"))

        logger.info("Waiting for task assignment (timeout=%.1fs)...", timeout_s)
        assignment = await client.await_assignment(timeout_s=timeout_s)
        if assignment.get("timeout") is True:
            raise TimeoutError(f"No task assigned to {settings.agent_name!r} within {timeout_s}s")
        if assignment.get("release") is True:
            logger.info("Worker received release without task assignment.")
            return {"released": True}

        task_id = assignment["task_id"]
        role = assignment["role"]
        instructions = assignment["instructions"]
        logger.info(
            "Received assignment: task_id=%s role=%r instructions=%r",
            task_id,
            role,
            instructions,
        )

        try:
            guide = await client.get_role_guide(role)
            logger.info("Retrieved role guide for %r (%d characters)", role, len(guide))
        except Exception as exc:
            logger.warning("Could not retrieve role guide for %r: %s", role, exc)

        await client.report_progress(task_id, f"Started working on {role} task")
        logger.info("Reported progress on task %s", task_id)

        if ask_question:
            logger.info("Asking Alice question: %r", ask_question)
            reply = await client.ask_alice(task_id, ask_question, timeout_s=30.0)
            logger.info("Alice replied: %s", reply)

        await client.report_progress(task_id, "Completed task execution, preparing result")

        dummy_sha = "0123456789abcdef0123456789abcdef01234567"
        if role == "reviewer":
            if fail:
                result = ReviewerResult(
                    verdict=ReviewerVerdict.FAILED,
                    summary="Review failed due to test execution error",
                )
            else:
                result = ReviewerResult(
                    verdict=ReviewerVerdict.APPROVED,
                    summary="All changes approved and verified",
                    reviewed_head_sha=dummy_sha,
                )
        else:
            if fail:
                result = ImplementerResult(
                    outcome=ImplementerOutcome.FAILED,
                    summary="Implementation failed: build errors",
                )
            else:
                result = ImplementerResult(
                    outcome=ImplementerOutcome.COMPLETED,
                    summary="Implementation completed successfully and verified",
                    pr_url=f"https://github.com/RoboNater/robo-agents/pull/{task_id[:4]}",
                    head_sha=dummy_sha,
                )

        logger.info("Submitting typed result: %s", type(result).__name__)
        submit_res = await client.submit_result(task_id, result)
        logger.info("Result submitted: status=%s", submit_res.get("status"))

        logger.info("Awaiting final release from Alice...")
        release_res = await client.await_assignment(timeout_s=timeout_s)
        if release_res.get("release") is True:
            logger.info("Worker %r received release. Work complete.", settings.agent_name)
        else:
            logger.info("Worker assignment poll returned: %s", release_res)

        return submit_res


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock worker coordinating with Alice")
    try:
        default_hub_url = str(HubSettings.from_env().public_url)
    except ConfigurationError:
        default_hub_url = "http://127.0.0.1:8420"

    parser.add_argument("--hub-url", default=default_hub_url, help="Hub public URL")
    parser.add_argument(
        "--token",
        default=os.environ.get("HUB_TOKEN", "test-token"),
        help="Bearer token for A2A communication",
    )
    parser.add_argument("--agent", default="bob", help="Worker agent name")
    parser.add_argument("--runtime", default="claude-code", help="Worker runtime slug")
    parser.add_argument("--ask", default=None, help="Optional question to ask Alice")
    parser.add_argument("--fail", action="store_true", help="Simulate task failure")
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Timeout in seconds waiting for assignments",
    )
    args = parser.parse_args()

    settings = WorkerSettings(
        hub_url=args.hub_url,
        token=args.token,
        agent_name=args.agent,
        profile=AgentProfile(harness=args.runtime),
        default_wait_s=args.timeout,
    )

    try:
        asyncio.run(
            run_worker(
                settings,
                ask_question=args.ask,
                fail=args.fail,
                timeout_s=args.timeout,
            )
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
