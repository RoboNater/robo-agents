from typing import Any
from unittest.mock import AsyncMock

from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings
from worker_mcp.tools import create_worker_mcp

EXPECTED_TOOLS = {
    "check_in",
    "get_role_guide",
    "await_assignment",
    "report_progress",
    "ask_alice",
    "submit_result",
}


async def test_worker_mcp_tools_list_and_dispatch() -> None:
    settings = WorkerSettings(
        hub_url="http://hub.example",
        token="tok",
        agent_name="bob",
    )
    client = WorkerHubClient(settings)
    client.check_in = AsyncMock(  # type: ignore[method-assign]
        return_value={"status": "registered", "agent": "bob", "context_id": "c1"}
    )
    client.get_role_guide = AsyncMock(return_value="# Guide\nContent")  # type: ignore[method-assign]
    client.await_assignment = AsyncMock(  # type: ignore[method-assign]
        return_value={"task_id": "t1", "role": "implementer", "instructions": "Do it"}
    )
    client.report_progress = AsyncMock(return_value={"ok": True, "note": "working"})  # type: ignore[method-assign]
    client.ask_alice = AsyncMock(return_value={"reply": "yes"})  # type: ignore[method-assign]
    client.submit_result = AsyncMock(return_value={"status": "completed", "task_id": "t1"})  # type: ignore[method-assign]

    server = create_worker_mcp(client)
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == EXPECTED_TOOLS

    async def call(name: str, **args: Any) -> Any:
        result = await server.call_tool(name, args)
        assert isinstance(result, tuple)
        return result[1]

    assert (await call("check_in", capabilities=["python"])) == {
        "status": "registered",
        "agent": "bob",
        "context_id": "c1",
    }
    assert (await call("get_role_guide", role="implementer")) == {
        "result": "# Guide\nContent"
    }
    assert (await call("await_assignment", timeout_s=60.0)) == {
        "task_id": "t1",
        "role": "implementer",
        "instructions": "Do it",
    }
    assert (await call("report_progress", task_id="t1", note="working")) == {
        "ok": True,
        "note": "working",
    }
    assert (await call("ask_alice", task_id="t1", question="is this ok?", timeout_s=30.0)) == {
        "reply": "yes",
    }
    assert (
        await call(
            "submit_result",
            task_id="t1",
            status="completed",
            summary="Done",
            artifacts=[{"name": "pr", "url": "https://pr"}],
        )
    ) == {"status": "completed", "task_id": "t1"}
