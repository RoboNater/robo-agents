import asyncio
from typing import Any

import httpx
import pytest
from agent_hub.store import HubStore
from agent_hub_common import EventKind, TaskState
from conftest import check_in, message, rpc, sse_results


async def post(client: httpx.AsyncClient, method: str, params: dict[str, Any]) -> Any:
    response = await client.post("/a2a", json=rpc(method, params))
    assert response.status_code == 200
    return response.json()


async def assigned_context(client: httpx.AsyncClient, store: HubStore) -> tuple[str, str]:
    """Check Bob in, give him a task, and return his context and task ids."""

    context_id = await check_in(client, "bob")
    task = store.assign_task("bob", "implementer", "Fix #1", "Open a PR")
    stream = await client.post(
        "/a2a", json=rpc("message/stream", message("NEXT", context_id=context_id))
    )
    assert sse_results(stream)[0]["id"] == task.id
    return context_id, task.id


async def test_check_in_returns_the_context_the_worker_must_use(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    body = await post(
        client,
        "message/send",
        message("READY", metadata={"agent": "bob", "capabilities": ["python"]}),
    )

    result = body["result"]
    agent = hub_store.agent_by_name("bob")
    assert result["kind"] == "message"
    assert result["metadata"]["agent"] == "bob"
    assert agent is not None and result["contextId"] == agent.context_id


async def test_a_message_with_no_task_must_be_the_check_in(client: httpx.AsyncClient) -> None:
    body = await post(client, "message/send", message("hello", metadata={"agent": "bob"}))

    assert body["error"]["code"] == -32602
    assert "READY" in body["error"]["message"]


async def test_check_in_needs_the_agent_name(client: httpx.AsyncClient) -> None:
    body = await post(client, "message/send", message("READY"))

    assert body["error"]["code"] == -32602


async def test_next_holds_until_alice_assigns(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id = await check_in(client, "bob")
    order: list[str] = []

    async def alice() -> str:
        await asyncio.sleep(0.02)
        order.append("assigned")
        return hub_store.assign_task("bob", "implementer", "Fix #1", "Open a PR").id

    async def worker() -> httpx.Response:
        response = await client.post(
            "/a2a", json=rpc("message/stream", message("NEXT", context_id=context_id))
        )
        order.append("received")
        return response

    response, task_id = await asyncio.gather(worker(), alice())

    result = sse_results(response)[0]
    assert response.headers["content-type"].startswith("text/event-stream")
    assert order == ["assigned", "received"]
    assert result["kind"] == "task"
    assert result["id"] == task_id
    assert result["status"]["state"] == "working"
    # The first message of the returned task is the assignment's instructions.
    assert result["status"]["message"]["parts"][0]["text"] == "Open a PR"
    assert result["metadata"]["role"] == "implementer"


async def test_next_returns_a_timeout_marker_when_nothing_is_assigned(
    client: httpx.AsyncClient,
) -> None:
    context_id = await check_in(client, "bob")

    response = await client.post(
        "/a2a",
        json=rpc(
            "message/stream",
            message("NEXT", context_id=context_id, metadata={"timeout_s": 0.05}),
        ),
    )

    result = sse_results(response)[0]
    assert result["metadata"]["timeout"] is True


async def test_next_reports_release(client: httpx.AsyncClient, hub_store: HubStore) -> None:
    context_id = await check_in(client, "bob")
    hub_store.release_agent("bob")

    response = await client.post(
        "/a2a", json=rpc("message/stream", message("NEXT", context_id=context_id))
    )

    result = sse_results(response)[0]
    assert result["metadata"]["release"] is True


async def test_next_rejects_an_unknown_context(client: httpx.AsyncClient) -> None:
    body = await post(client, "message/stream", message("NEXT", context_id="nope"))

    assert body["error"]["code"] == -32602


async def test_progress_is_acknowledged_and_queued_for_alice(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)
    while hub_store.next_event():
        pass

    body = await post(
        client,
        "message/send",
        message(
            "branch pushed",
            context_id=context_id,
            task_id=task_id,
            metadata={"kind": "progress"},
        ),
    )

    event = hub_store.next_event()
    assert body["result"]["metadata"]["kind"] == "progress_ack"
    assert event is not None and event.kind is EventKind.TASK_PROGRESS
    assert event.payload["note"] == "branch pushed"


async def test_a_question_holds_until_alice_replies(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)
    while hub_store.next_event():
        pass

    async def alice() -> None:
        event = await hub_store.wait_for_event(2.0)
        assert event is not None and event.kind is EventKind.WORKER_QUESTION
        hub_store.reply(event.payload["task_id"], "main")

    async def worker() -> httpx.Response:
        return await client.post(
            "/a2a",
            json=rpc(
                "message/stream",
                message(
                    "Which base branch?",
                    context_id=context_id,
                    task_id=task_id,
                    metadata={"kind": "question"},
                ),
            ),
        )

    response, _ = await asyncio.gather(worker(), alice())

    result = sse_results(response)[0]
    task = hub_store.get_task(task_id)
    assert result["parts"][0]["text"] == "main"
    assert task is not None and task.state is TaskState.WORKING


async def test_an_unanswered_question_times_out_and_stays_parked(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)

    response = await client.post(
        "/a2a",
        json=rpc(
            "message/stream",
            message(
                "Which base branch?",
                context_id=context_id,
                task_id=task_id,
                metadata={"kind": "question", "timeout_s": 0.05},
            ),
        ),
    )

    task = hub_store.get_task(task_id)
    assert sse_results(response)[0]["metadata"]["timeout"] is True
    assert task is not None and task.state is TaskState.INPUT_REQUIRED


async def test_a_retried_question_still_receives_a_reply_sent_in_the_gap(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)
    while hub_store.next_event():
        pass
    ask = rpc(
        "message/stream",
        message(
            "Which base branch?",
            context_id=context_id,
            task_id=task_id,
            metadata={"kind": "question", "timeout_s": 0.05},
        ),
    )

    timed_out = await client.post("/a2a", json=ask)
    # Alice answers after the hold elapsed but before the worker calls again.
    hub_store.reply(task_id, "main")
    retried = await client.post("/a2a", json=ask)

    marker = sse_results(timed_out)[0]
    assert marker["metadata"]["timeout"] is True
    # The marker names the id the retry has to be sent under.
    assert marker["metadata"]["retry_as_message_id"] == ask["params"]["message"]["messageId"]
    assert sse_results(retried)[0]["parts"][0]["text"] == "main"
    assert hub_store.next_event() is not None
    assert hub_store.next_event() is None


async def test_a_worker_released_while_away_is_released_when_it_returns(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    await check_in(client, "bob")
    hub_store.release_agent("bob")

    # The worker restarts and re-announces itself, as after a crash.
    context_id = await check_in(client, "bob")
    response = await client.post(
        "/a2a", json=rpc("message/stream", message("NEXT", context_id=context_id))
    )

    assert sse_results(response)[0]["metadata"]["release"] is True


async def test_a_streaming_call_on_a_task_must_be_a_question(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)

    body = await post(
        client,
        "message/stream",
        message("hi", context_id=context_id, task_id=task_id, metadata={"kind": "progress"}),
    )

    assert body["error"]["code"] == -32602


async def test_a_result_ends_the_task_and_carries_its_artifacts(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)
    while hub_store.next_event():
        pass

    body = await post(
        client,
        "message/send",
        message(
            "PR ready for review",
            context_id=context_id,
            task_id=task_id,
            metadata={
                "kind": "result",
                "status": "completed",
                "artifacts": [{"name": "pr", "url": "https://example.test/pr/1"}],
            },
        ),
    )

    result = body["result"]
    event = hub_store.next_event()
    assert result["status"]["state"] == "completed"
    assert "https://example.test/pr/1" in result["artifacts"][0]["parts"][0]["text"]
    assert event is not None and event.kind is EventKind.TASK_COMPLETED
    assert event.payload["summary"] == "PR ready for review"


async def test_a_failed_result_is_reported_as_such(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)

    body = await post(
        client,
        "message/send",
        message(
            "tests will not pass",
            context_id=context_id,
            task_id=task_id,
            metadata={"kind": "result", "status": "failed"},
        ),
    )

    assert body["result"]["status"]["state"] == "failed"


@pytest.mark.parametrize("status", ["unknown", "working", None])
async def test_a_result_needs_a_terminal_status(
    client: httpx.AsyncClient, hub_store: HubStore, status: str | None
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)

    body = await post(
        client,
        "message/send",
        message(
            "done",
            context_id=context_id,
            task_id=task_id,
            metadata={"kind": "result", "status": status},
        ),
    )

    assert "error" in body


async def test_a_worker_cannot_act_on_another_workers_task(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    _, task_id = await assigned_context(client, hub_store)
    charlie = await check_in(client, "charlie")

    body = await post(
        client,
        "message/send",
        message("mine now", context_id=charlie, task_id=task_id, metadata={"kind": "progress"}),
    )

    assert body["error"]["code"] == -32602
    assert "not assigned" in body["error"]["message"]


async def test_tasks_get_returns_the_task_with_its_transcript(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)
    hub_store.record_progress(task_id, "bob", "branch pushed")

    body = await post(client, "tasks/get", {"id": task_id, "historyLength": 1})

    result = body["result"]
    assert result["id"] == task_id
    assert result["contextId"] == context_id
    assert [part["text"] for part in result["history"][0]["parts"]] == ["branch pushed"]


async def test_tasks_get_reports_an_unknown_task(client: httpx.AsyncClient) -> None:
    body = await post(client, "tasks/get", {"id": "missing"})

    assert body["error"]["code"] == -32001


async def test_tasks_cancel_ends_an_open_task_once(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    _, task_id = await assigned_context(client, hub_store)

    canceled = await post(client, "tasks/cancel", {"id": task_id})
    again = await post(client, "tasks/cancel", {"id": task_id})

    assert canceled["result"]["status"]["state"] == "canceled"
    assert again["error"]["code"] == -32002


async def test_unknown_methods_and_malformed_bodies_are_rejected(
    client: httpx.AsyncClient,
) -> None:
    unknown = await post(client, "tasks/resubscribe", {"id": "t"})
    unparsable = await client.post(
        "/a2a", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    shapeless = await client.post("/a2a", json=[1, 2, 3])
    missing_params = await client.post(
        "/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get"}
    )

    assert unknown["error"]["code"] == -32601
    assert unparsable.json()["error"]["code"] == -32700
    assert shapeless.json()["error"]["code"] == -32600
    assert missing_params.json()["error"]["code"] == -32602


async def test_a_requested_timeout_is_clamped_to_the_configured_ceiling(
    client: httpx.AsyncClient,
) -> None:
    context_id = await check_in(client, "bob")

    # The ceiling is 1s in the test settings; without clamping this would hang.
    response = await client.post(
        "/a2a",
        json=rpc(
            "message/stream",
            message("NEXT", context_id=context_id, metadata={"timeout_s": 9000}),
        ),
        timeout=10,
    )

    assert sse_results(response)[0]["metadata"]["timeout"] is True


async def test_a_non_numeric_timeout_is_rejected(client: httpx.AsyncClient) -> None:
    context_id = await check_in(client, "bob")

    body = await post(
        client,
        "message/stream",
        message("NEXT", context_id=context_id, metadata={"timeout_s": "soon"}),
    )

    assert body["error"]["code"] == -32602
