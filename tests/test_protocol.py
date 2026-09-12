import asyncio
from typing import Any

import httpx
import pytest
from agent_hub.store import HubStore
from agent_hub_common import UNKNOWN, EventKind, MetaKeys, ModelSource, TaskState
from conftest import check_in, message, rpc, sse_results


async def post(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, Any],
    status_code: int = 200,
) -> Any:
    response = await client.post("/a2a", json=rpc(method, params))
    assert response.status_code == status_code
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
        message(
            "READY",
            metadata={
                MetaKeys.AGENT: "bob",
                MetaKeys.CAPABILITIES: ["python"],
                MetaKeys.SCHEMA_VERSION: 1,
                MetaKeys.OPERATION_ID: "op-proto-checkin-1",
            },
        ),
    )

    result = body["result"]
    agent = hub_store.agent_by_name("bob")
    assert result["kind"] == "message"
    assert result["metadata"][MetaKeys.AGENT] == "bob"
    assert agent is not None and result["contextId"] == agent.context_id


async def test_a_second_live_worker_with_the_same_name_gets_conflict(
    client: httpx.AsyncClient,
) -> None:
    await check_in(client, "bob", worker_instance_id="bob-1")

    body = await post(
        client,
        "message/send",
        message(
            "READY",
            metadata={
                MetaKeys.AGENT: "bob",
                MetaKeys.SCHEMA_VERSION: 1,
                MetaKeys.OPERATION_ID: "op-duplicate-bob",
                MetaKeys.WORKER_INSTANCE_ID: "bob-2",
            },
        ),
        status_code=409,
    )

    assert body["error"]["code"] == -32600
    assert "live worker instance" in body["error"]["message"]


async def test_heartbeat_is_an_immediate_message_send_intent(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id = await check_in(client, "bob", worker_instance_id="bob-1")
    before = hub_store.agent_by_name("bob")
    assert before is not None

    body = await post(
        client,
        "message/send",
        message(
            "HEARTBEAT",
            context_id=context_id,
            metadata={
                MetaKeys.KIND: "heartbeat",
                MetaKeys.AGENT: "bob",
                MetaKeys.SCHEMA_VERSION: 1,
                MetaKeys.WORKER_INSTANCE_ID: "bob-1",
            },
        ),
    )

    after = hub_store.agent_by_name("bob")
    assert body["result"]["metadata"] == {
        MetaKeys.KIND: "heartbeat_ack",
        MetaKeys.AGENT: "bob",
        MetaKeys.ACCEPTED: True,
    }
    assert after is not None and after.last_heartbeat >= before.last_heartbeat


async def test_worker_calls_require_the_current_instance_id(
    client: httpx.AsyncClient,
) -> None:
    context_id = await check_in(client, "bob", worker_instance_id="bob-1")
    params = message("NEXT", context_id=context_id)
    params["message"]["metadata"].pop(MetaKeys.WORKER_INSTANCE_ID)

    body = await post(client, "message/stream", params, status_code=400)

    assert body["error"]["code"] == -32602
    assert "worker_instance_id" in body["error"]["message"]


async def test_a_message_with_no_task_must_be_the_check_in(client: httpx.AsyncClient) -> None:
    body = await post(
        client, "message/send", message("hello", metadata={MetaKeys.AGENT: "bob"})
    )

    assert body["error"]["code"] == -32602
    assert "READY" in body["error"]["message"]


async def test_check_in_needs_the_agent_name(client: httpx.AsyncClient) -> None:
    body = await post(
        client,
        "message/send",
        message(
            "READY",
            metadata={
                MetaKeys.SCHEMA_VERSION: 1,
                MetaKeys.OPERATION_ID: "op-proto-checkin-2",
            },
        ),
    )

    assert body["error"]["code"] == -32602
    assert "agent" in body["error"]["message"].lower()


async def test_check_in_records_the_reported_profile(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    body = await post(
        client,
        "message/send",
        message(
            "READY",
            metadata={
                MetaKeys.AGENT: "charlie",
                MetaKeys.CAPABILITIES: ["python", " gh ", "python", ""],
                MetaKeys.HARNESS: "codex",
                MetaKeys.HARNESS_VERSION: "0.154.0",
                MetaKeys.PROVIDER: " openai ",
                MetaKeys.MODEL: "example-codex-model",
                MetaKeys.MODEL_SOURCE: "declared",
                MetaKeys.WORKSPACE_ID: "ws-charlie",
                MetaKeys.SCHEMA_VERSION: 1,
                MetaKeys.OPERATION_ID: "op-proto-profile-1",
            },
        ),
    )

    assert "error" not in body
    agent = hub_store.agent_by_name("charlie")
    assert agent is not None
    assert agent.capabilities == ["python", "gh"]
    assert (agent.harness, agent.harness_version, agent.provider) == ("codex", "0.154.0", "openai")
    assert (agent.model, agent.model_source) == ("example-codex-model", ModelSource.DECLARED)
    assert agent.workspace_id == "ws-charlie"


async def test_a_check_in_without_a_profile_records_unknown(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    # A source with no model describes nothing, so it is dropped too.
    metadata: dict[str, Any] = {
        MetaKeys.AGENT: "bob",
        MetaKeys.HARNESS: "",
        MetaKeys.MODEL_SOURCE: "env",
        MetaKeys.SCHEMA_VERSION: 1,
        MetaKeys.OPERATION_ID: "op-proto-profile-unknown",
    }
    body = await post(client, "message/send", message("READY", metadata=metadata))

    assert "error" not in body
    agent = hub_store.agent_by_name("bob")
    assert agent is not None
    assert (agent.harness, agent.harness_version, agent.provider, agent.model) == (
        UNKNOWN,
        UNKNOWN,
        UNKNOWN,
        UNKNOWN,
    )
    assert agent.model_source is ModelSource.UNKNOWN
    assert agent.capabilities == []
    assert agent.workspace_id is None


@pytest.mark.parametrize(
    ("profile", "complaint"),
    [
        ({MetaKeys.HARNESS: ["codex"]}, MetaKeys.HARNESS),
        ({MetaKeys.MODEL: "m"}, MetaKeys.MODEL_SOURCE),
        ({MetaKeys.MODEL: "m", MetaKeys.MODEL_SOURCE: "unknown"}, MetaKeys.MODEL_SOURCE),
        ({MetaKeys.MODEL: "m", MetaKeys.MODEL_SOURCE: "guessed"}, MetaKeys.MODEL_SOURCE),
        ({MetaKeys.CAPABILITIES: "python"}, MetaKeys.CAPABILITIES),
    ],
)
async def test_a_malformed_profile_is_refused(
    client: httpx.AsyncClient, hub_store: HubStore, profile: dict[str, Any], complaint: str
) -> None:
    metadata = {
        MetaKeys.AGENT: "bob",
        MetaKeys.SCHEMA_VERSION: 1,
        MetaKeys.OPERATION_ID: "op-proto-profile-malformed",
        **profile,
    }
    body = await post(client, "message/send", message("READY", metadata=metadata))

    assert body["error"]["code"] == -32602
    assert complaint in body["error"]["message"]
    assert hub_store.agent_by_name("bob") is None


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
    assert result["metadata"][MetaKeys.ROLE] == "implementer"


async def test_next_returns_a_timeout_marker_when_nothing_is_assigned(
    client: httpx.AsyncClient,
) -> None:
    context_id = await check_in(client, "bob")

    response = await client.post(
        "/a2a",
        json=rpc(
            "message/stream",
            message("NEXT", context_id=context_id, metadata={MetaKeys.TIMEOUT_S: 0.05}),
        ),
    )

    result = sse_results(response)[0]
    assert result["metadata"][MetaKeys.TIMEOUT] is True


async def test_next_reports_release(client: httpx.AsyncClient, hub_store: HubStore) -> None:
    context_id = await check_in(client, "bob")
    hub_store.release_agent("bob")

    response = await client.post(
        "/a2a", json=rpc("message/stream", message("NEXT", context_id=context_id))
    )

    result = sse_results(response)[0]
    assert result["metadata"][MetaKeys.RELEASE] is True


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
            metadata={
                MetaKeys.KIND: "progress",
                MetaKeys.SCHEMA_VERSION: 1,
                MetaKeys.OPERATION_ID: "op-proto-prog-ack",
            },
        ),
    )

    event = hub_store.next_event()
    assert body["result"]["metadata"][MetaKeys.KIND] == "progress_ack"
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
                    metadata={MetaKeys.KIND: "question"},
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
                metadata={MetaKeys.KIND: "question", MetaKeys.TIMEOUT_S: 0.05},
            ),
        ),
    )

    task = hub_store.get_task(task_id)
    assert sse_results(response)[0]["metadata"][MetaKeys.TIMEOUT] is True
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
            metadata={MetaKeys.KIND: "question", MetaKeys.TIMEOUT_S: 0.05},
        ),
    )

    timed_out = await client.post("/a2a", json=ask)
    # Alice answers after the hold elapsed but before the worker calls again.
    hub_store.reply(task_id, "main")
    retried = await client.post("/a2a", json=ask)

    marker = sse_results(timed_out)[0]
    assert marker["metadata"][MetaKeys.TIMEOUT] is True
    # The marker names the id the retry has to be sent under.
    assert marker["metadata"][MetaKeys.RETRY_AS_MESSAGE_ID] == ask["params"]["message"]["messageId"]
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

    assert sse_results(response)[0]["metadata"][MetaKeys.RELEASE] is True


async def test_a_streaming_call_on_a_task_must_be_a_question(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)

    body = await post(
        client,
        "message/stream",
        message("hi", context_id=context_id, task_id=task_id, metadata={MetaKeys.KIND: "progress"}),
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
                MetaKeys.KIND: "result",
                MetaKeys.SCHEMA_VERSION: 1,
                MetaKeys.OPERATION_ID: "op-proto-res-1",
                MetaKeys.RESULT: {
                    "outcome": "completed",
                    "summary": "PR ready for review",
                    "pr_url": "https://example.test/pr/1",
                    "head_sha": "0123456789abcdef0123456789abcdef01234567",
                },
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
            metadata={
                MetaKeys.KIND: "result",
                MetaKeys.SCHEMA_VERSION: 1,
                MetaKeys.OPERATION_ID: "op-proto-res-2",
                MetaKeys.RESULT: {
                    "outcome": "failed",
                    "summary": "tests will not pass",
                },
            },
        ),
    )

    assert body["result"]["status"]["state"] == "failed"


@pytest.mark.parametrize(
    "bad_result",
    [
        "not a dict",
        None,
        {"outcome": "invalid_outcome", "summary": "bad"},
    ],
)
async def test_a_result_requires_typed_result(
    client: httpx.AsyncClient, hub_store: HubStore, bad_result: Any
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)

    meta: dict[str, Any] = {
        MetaKeys.KIND: "result",
        MetaKeys.SCHEMA_VERSION: 1,
        MetaKeys.OPERATION_ID: "op-proto-res-bad",
    }
    if bad_result is not None:
        meta[MetaKeys.RESULT] = bad_result

    body = await post(
        client,
        "message/send",
        message(
            "done",
            context_id=context_id,
            task_id=task_id,
            metadata=meta,
        ),
        status_code=400,
    )

    assert "error" in body
    assert body["error"]["code"] == -32602


HEAD = "0123456789abcdef0123456789abcdef01234567"


async def assigned_rebase(client: httpx.AsyncClient, store: HubStore) -> tuple[str, dict[str, Any]]:
    """Check Bob in, give him a rebase bound to HEAD, and return what NEXT delivered."""

    context_id = await check_in(client, "bob")
    store.assign_task("bob", "rebase", "Rebase #7", "Bring #7 up to date", pr_head_sha=HEAD)
    stream = await client.post(
        "/a2a", json=rpc("message/stream", message("NEXT", context_id=context_id))
    )
    return context_id, sse_results(stream)[0]


async def test_a_rebase_assignment_carries_the_head_it_is_bound_to(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    _, task = await assigned_rebase(client, hub_store)

    assert task["metadata"][MetaKeys.ROLE] == "rebase"
    assert task["metadata"][MetaKeys.PR_HEAD_SHA] == HEAD
    assert task["status"]["message"]["metadata"][MetaKeys.PR_HEAD_SHA] == HEAD


def rebase_result(result: dict[str, Any], operation_id: str) -> dict[str, Any]:
    return {
        MetaKeys.KIND: "result",
        MetaKeys.SCHEMA_VERSION: 1,
        MetaKeys.OPERATION_ID: operation_id,
        MetaKeys.RESULT: result,
    }


async def test_a_rebase_result_is_validated_as_a_rebase(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task = await assigned_rebase(client, hub_store)
    # No pr_url: an implementer result would be refused for that; a rebase
    # of an existing PR needs none.
    result = {"outcome": "completed", "head_sha": HEAD, "summary": "Rebased, no conflicts"}

    body = await post(
        client,
        "message/send",
        message(
            "Rebased",
            context_id=context_id,
            task_id=task["id"],
            metadata=rebase_result(result, "op-rebase-1"),
        ),
    )

    stored = hub_store.get_task(task["id"])
    assert body["result"]["status"]["state"] == "completed"
    assert stored is not None and stored.result is not None
    assert stored.result["conflict_files"] == []
    assert stored.result["resolution_summary"] is None


async def test_a_rebase_claiming_conflicts_must_explain_them(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id, task = await assigned_rebase(client, hub_store)
    result = {
        "outcome": "completed",
        "head_sha": HEAD,
        "conflict_files": ["packages/hub/src/agent_hub/database.py"],
        "summary": "Rebased",
    }

    body = await post(
        client,
        "message/send",
        message(
            "Rebased",
            context_id=context_id,
            task_id=task["id"],
            metadata=rebase_result(result, "op-rebase-2"),
        ),
        status_code=400,
    )

    stored = hub_store.get_task(task["id"])
    assert "resolution_summary" in body["error"]["message"]
    assert stored is not None and stored.state is TaskState.WORKING


async def test_a_worker_cannot_act_on_another_workers_task(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    _, task_id = await assigned_context(client, hub_store)
    charlie = await check_in(client, "charlie")

    body = await post(
        client,
        "message/send",
        message(
            "mine now",
            context_id=charlie,
            task_id=task_id,
            metadata={
                MetaKeys.KIND: "progress",
                MetaKeys.SCHEMA_VERSION: 1,
                MetaKeys.OPERATION_ID: "op-act-1",
            },
        ),
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
            message("NEXT", context_id=context_id, metadata={MetaKeys.TIMEOUT_S: 9000}),
        ),
        timeout=10,
    )

    assert sse_results(response)[0]["metadata"][MetaKeys.TIMEOUT] is True


async def test_a_non_numeric_timeout_is_rejected(client: httpx.AsyncClient) -> None:
    context_id = await check_in(client, "bob")

    body = await post(
        client,
        "message/stream",
        message("NEXT", context_id=context_id, metadata={MetaKeys.TIMEOUT_S: "soon"}),
    )

    assert body["error"]["code"] == -32602


@pytest.mark.parametrize("state", [TaskState.CANCELED, TaskState.FAILED])
async def test_manual_override_returns_terminal_task_to_question_stream(
    client: httpx.AsyncClient, hub_store: HubStore, state: TaskState
) -> None:
    context_id, task_id = await assigned_context(client, hub_store)
    pending = asyncio.create_task(
        client.post(
            "/a2a",
            json=rpc(
                "message/stream",
                message(
                    "Which?",
                    context_id=context_id,
                    task_id=task_id,
                    metadata={MetaKeys.KIND: "question"},
                ),
            ),
        )
    )
    while True:
        task = hub_store.get_task(task_id)
        assert task is not None
        if task.state == TaskState.INPUT_REQUIRED:
            break
        await asyncio.sleep(0)
    hub_store.set_task_state(task_id, state, "Superseded")
    result = sse_results(await asyncio.wait_for(pending, 1))[0]
    assert result["kind"] == "task"
    assert result["status"]["state"] == state.value
    assert result["status"]["message"]["parts"][0]["text"] == "Superseded"
    assert result["status"]["message"]["metadata"] == {
        MetaKeys.KIND: "state_override",
        MetaKeys.STATE: state.value,
    }
    assert result["metadata"][MetaKeys.RESULT]["summary"] == "Superseded"
