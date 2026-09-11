"""Worker MCP tools (§4.3) exposed to LLM runtimes."""

from __future__ import annotations

from typing import Annotated, Any

from agent_hub_common import ImplementerResult, ReviewerResult
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .client import WorkerHubClient

Timeout = Annotated[float, Field(ge=0, le=300, allow_inf_nan=False)]


def create_worker_mcp(client: WorkerHubClient) -> FastMCP:
    server = FastMCP(
        "worker-mcp",
        instructions=(
            "Worker-side MCP tools for coordinating with Alice through the hub. "
            "Follow the workflow: check_in -> await_assignment -> get_role_guide -> "
            "do work -> submit_result."
        ),
    )

    @server.tool()
    async def check_in(
        capabilities: list[str] | None = None, model: str | None = None
    ) -> dict[str, Any]:
        """One-time registration with the hub, reporting this worker's identity profile.

        Harness, provider and configured capabilities come from the launcher.
        capabilities: optional extra capabilities to declare.
        model: your exact model ID if you know it; ignored when the launcher
        already names the model. Omit rather than guess.
        """
        return await client.check_in(capabilities, model)

    @server.tool()
    async def get_role_guide(role: str) -> str:
        """Fetch instructions for the assigned role (e.g. 'implementer', 'reviewer')."""
        return await client.get_role_guide(role)

    @server.tool()
    async def await_assignment(timeout_s: Timeout | None = None) -> dict[str, Any]:
        """Poll the hub for the next task assignment.

        Returns {task_id, role, instructions}, {release: true}, or {timeout: true}.
        On timeout, call again.
        timeout_s: Optional wait timeout in seconds (defaults to HUB_DEFAULT_WAIT_S if omitted).
        """
        return await client.await_assignment(timeout_s)

    @server.tool()
    async def report_progress(task_id: str, note: str) -> dict[str, Any]:
        """Send a non-blocking progress update note to Alice."""
        return await client.report_progress(task_id, note)

    @server.tool()
    async def ask_alice(
        task_id: str, question: str, timeout_s: Timeout | None = None
    ) -> dict[str, Any]:
        """Ask Alice a clarifying question and hold for her response.

        Returns {reply: text}, {timeout: true}, or {task_ended: true, state, note}.
        On timeout, call ask_alice again to continue waiting; retries resume the pending question.
        timeout_s: Optional wait timeout in seconds (defaults to HUB_DEFAULT_WAIT_S if omitted).
        """
        return await client.ask_alice(task_id, question, timeout_s)

    @server.tool()
    async def submit_result(
        task_id: str,
        result: ImplementerResult | ReviewerResult,
    ) -> dict[str, Any]:
        """Submit the final result for a task, validated against the role's schema."""
        return await client.submit_result(task_id, result)

    return server
