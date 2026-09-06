import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_hub.database import database, initialize_database
from agent_hub.mcp import create_mcp
from agent_hub.store import HubStore
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = {
    "get_state",
    "wait_for_event",
    "assign_task",
    "reply",
    "set_task_state",
    "release_agent",
    "set_workflow_status",
    "log_decision",
}


async def test_tools_and_durable_actions(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)
    store = HubStore(path)
    server = create_mcp(store)

    async def call(name: str, **args: Any) -> Any:
        result = await server.call_tool(name, args)
        assert isinstance(result, tuple)
        return result[1]

    assert {t.name for t in await server.list_tools()} == TOOLS
    assert (await call("get_state"))["workflow"] is None
    bob = store.check_in("bob", ["python"])
    pending = asyncio.create_task(store.await_assignment(bob.context_id, 1))
    await asyncio.sleep(0)
    task = await call(
        "assign_task", agent="bob", role="implementer", title="Fix", instructions="Do it"
    )
    claimed = await pending
    assert claimed is not None
    question = store.open_question(task["id"], "bob", "Which?", "q1")
    waiting = asyncio.create_task(store.await_reply(task["id"], question, 1))
    await asyncio.sleep(0)
    await call("reply", task_id=task["id"], text="This one")
    answer = await waiting
    assert answer is not None and answer.parts[0]["text"] == "This one"
    await call("set_task_state", task_id=task["id"], state="failed", note="Stop")
    assert store.agent_by_name("bob").current_task_id is None  # type: ignore[union-attr]
    await call("release_agent", agent="bob")
    await call("set_workflow_status", status="done", summary="Finished")
    await call("log_decision", summary="Decision", rationale="Because")
    state = HubStore(path).get_state()
    assert state["workflow"]["status"] == "done"
    assert state["tasks"][0]["result"]["summary"] == "Stop"
    assert "instructions" not in state["tasks"][0]
    with database(path) as conn:
        assert (
            conn.execute("SELECT summary FROM decision ORDER BY id").fetchall()[0][0] == "Finished"
        )
    for args in ({"timeout_s": -1}, {"timeout_s": 121}, {"timeout_s": float("inf")}):
        with pytest.raises(Exception, match="validation error"):
            await call("wait_for_event", **args)
    with pytest.raises(Exception, match="already failed"):
        await call("set_task_state", task_id=task["id"], state="canceled", note="Again")


async def test_stdio_and_http_share_events(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_hub.main"],
        env={
            **os.environ,
            "HUB_STATE_DIR": str(tmp_path),
            "HUB_DB_PATH": str(tmp_path / "hub.db"),
            "HUB_HOST": "127.0.0.1",
            "HUB_PORT": str(port),
            "HUB_TOKEN": "test",
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert {t.name for t in (await session.list_tools()).tools} == TOOLS
        timeout = await session.call_tool("wait_for_event", {"timeout_s": 0.02})
        assert timeout.structuredContent == {"event": None}
        pending = asyncio.create_task(session.call_tool("wait_for_event", {"timeout_s": 2}))
        await asyncio.sleep(0.05)
        assert not pending.done()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/a2a",
                headers={"Authorization": "Bearer test"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "messageId": "ready",
                            "role": "user",
                            "parts": [{"kind": "text", "text": "READY"}],
                            "metadata": {"agent": "bob", "capabilities": ["python"]},
                        }
                    },
                },
            )
            assert response.status_code == 200
        result = await pending
        assert result.structuredContent is not None
        assert result.structuredContent["event"]["kind"] == "agent_checked_in"
        empty = await session.call_tool("wait_for_event", {"timeout_s": 0})
        assert empty.structuredContent == {"event": None}
        error = await session.call_tool("reply", {"task_id": "missing", "text": "hi"})
        assert error.isError
        assert json.loads((await session.call_tool("get_state")).content[0].text)["agents"]  # type: ignore[union-attr]
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"http://127.0.0.1:{port}/healthz")
