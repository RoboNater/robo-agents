import asyncio

import httpx
import pytest
from agent_hub.store import HubStore
from agent_hub_common import HubSettings, TaskState
from conftest import BASE_URL, TOKEN
from worker_mcp.client import WorkerHubClient, WorkerProtocolError
from worker_mcp.config import WorkerSettings


@pytest.fixture
def worker_settings() -> WorkerSettings:
    return WorkerSettings(
        hub_url=BASE_URL,
        token=TOKEN,
        agent_name="bob",
        runtime="claude-code",
        default_wait_s=0.2,
        max_retries=2,
        backoff_factor_s=0.01,
    )


async def test_worker_check_in_and_state(
    client: httpx.AsyncClient, worker_settings: WorkerSettings, hub_store: HubStore
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    res = await worker.check_in(["python", "testing"])

    assert res["status"] == "registered"
    assert res["agent"] == "bob"
    assert worker.context_id is not None
    assert hub_store.agent_by_name("bob") is not None


async def test_get_role_guide_no_cache(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    settings: HubSettings,
) -> None:
    settings.guides_dir.mkdir(parents=True, exist_ok=True)
    guide_file = settings.guides_dir / "implementer.md"
    guide_file.write_text("# Implementer Guide\nDo the work.", encoding="utf-8")

    worker = WorkerHubClient(worker_settings, http_client=client)

    # Invalid slug rejection
    with pytest.raises(ValueError, match="Role must be a slug"):
        await worker.get_role_guide("Implementer")
    with pytest.raises(ValueError, match="Role must be a slug"):
        await worker.get_role_guide("../traversal")

    # Unknown guide -> 404 / FileNotFoundError
    with pytest.raises(FileNotFoundError, match="not found"):
        await worker.get_role_guide("reviewer")

    # Fetch guide
    content = await worker.get_role_guide("implementer")
    assert "Implementer Guide" in content

    # Modify guide file and verify no local cache (fetches freshly each time)
    guide_file.write_text("# Updated Guide\nNew instructions.", encoding="utf-8")
    updated = await worker.get_role_guide("implementer")
    assert "Updated Guide" in updated


async def test_await_assignment_and_release(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)

    # Calling before check-in raises error
    with pytest.raises(RuntimeError, match="call check_in first"):
        await worker.await_assignment()

    await worker.check_in()

    # Timeout when no assignment pending
    timeout_res = await worker.await_assignment(timeout_s=0.05)
    assert timeout_res == {"timeout": True}

    # Assignment wake-up
    async def assign() -> None:
        await asyncio.sleep(0.02)
        hub_store.assign_task("bob", "implementer", "Fix issue #42", "Write code and tests")

    assignment_task = asyncio.create_task(worker.await_assignment(timeout_s=2.0))
    await asyncio.gather(assign(), assignment_task)

    assignment = assignment_task.result()
    assert assignment["role"] == "implementer"
    assert "Write code and tests" in assignment["instructions"]
    assert assignment["task_id"] != ""

    # Release
    hub_store.release_agent("bob")
    release_res = await worker.await_assignment(timeout_s=2.0)
    assert release_res == {"release": True}


async def test_report_progress_and_submit_result(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Do it")

    # Progress
    prog_res = await worker.report_progress(task.id, "50% done")
    assert prog_res == {"ok": True, "note": "50% done"}
    event = hub_store.next_event()
    while event and event.payload.get("task_id") != task.id:
        event = hub_store.next_event()
    assert event is not None and event.payload.get("note") == "50% done"

    # Submit result
    with pytest.raises(ValueError, match="status must be 'completed' or 'failed'"):
        await worker.submit_result(task.id, "unknown", "bad status")

    res = await worker.submit_result(
        task.id,
        "completed",
        "Finished successfully",
        artifacts=[{"name": "pr", "url": "https://github.com/pr/1"}],
    )
    assert res["status"] == "completed"
    assert res["task_id"] == task.id
    stored_task = hub_store.get_task(task.id)
    assert stored_task is not None and stored_task.state == TaskState.COMPLETED


async def test_ask_alice_reply_and_retry_correlation(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    # 1. Ask question with answer
    async def answer() -> None:
        for _ in range(100):
            current = hub_store.get_task(task.id)
            if current is not None and current.state == TaskState.INPUT_REQUIRED:
                break
            await asyncio.sleep(0.01)
        hub_store.reply(task.id, "Use SQLite for persistence.")

    ask_task = asyncio.create_task(worker.ask_alice(task.id, "Which database?", timeout_s=2.0))
    await asyncio.gather(answer(), ask_task)
    reply = ask_task.result()
    assert reply == {"reply": "Use SQLite for persistence."}

    # 2. Ask question that times out
    timeout_res = await worker.ask_alice(task.id, "Second question?", timeout_s=0.05)
    assert timeout_res == {"timeout": True}
    assert task.id in worker._pending_questions
    saved_msg_id = worker._pending_questions[task.id][1]
    assert saved_msg_id != ""

    # Alice answers while worker is retrying in the gap
    hub_store.reply(task.id, "The answer given in the gap.")

    # 3. Retry uses the saved message_id and picks up the answer
    retry_res = await worker.ask_alice(task.id, "Second question?", timeout_s=2.0)
    assert retry_res == {"reply": "The answer given in the gap."}
    assert task.id not in worker._pending_questions

    # 4. Asking a different question after timeout generates a new message_id
    timeout_diff_1 = await worker.ask_alice(task.id, "Question A?", timeout_s=0.05)
    assert timeout_diff_1 == {"timeout": True}
    msg_id_a = worker._pending_questions[task.id][1]

    timeout_diff_2 = await worker.ask_alice(task.id, "Question B?", timeout_s=0.05)
    assert timeout_diff_2 == {"timeout": True}
    msg_id_b = worker._pending_questions[task.id][1]
    assert msg_id_b != msg_id_a


async def test_ask_alice_manual_termination_override(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    async def cancel_task() -> None:
        for _ in range(100):
            current = hub_store.get_task(task.id)
            if current is not None and current.state == TaskState.INPUT_REQUIRED:
                break
            await asyncio.sleep(0.01)
        hub_store.set_task_state(task.id, TaskState.CANCELED, "Task aborted by user")

    ask_task = asyncio.create_task(worker.ask_alice(task.id, "How to proceed?", timeout_s=2.0))
    await asyncio.gather(cancel_task(), ask_task)

    res = ask_task.result()
    assert res.get("task_ended") is True
    assert res.get("state") == "canceled"
    assert "aborted" in str(res.get("note"))
    assert task.id not in worker._pending_questions


async def test_ask_alice_cancellation_without_result_summary(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    async def cancel_task() -> None:
        for _ in range(100):
            current = hub_store.get_task(task.id)
            if current is not None and current.state == TaskState.INPUT_REQUIRED:
                break
            await asyncio.sleep(0.01)
        hub_store.cancel_task(task.id)

    ask_task = asyncio.create_task(worker.ask_alice(task.id, "How to proceed?", timeout_s=2.0))
    await asyncio.gather(cancel_task(), ask_task)

    res = ask_task.result()
    assert res.get("task_ended") is True
    assert res.get("state") == "canceled"
    assert res.get("note") == "canceled"
    assert task.id not in worker._pending_questions


async def test_stream_rpc_json_error_raises_protocol_error(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    # 1. await_assignment with unknown contextId raises WorkerProtocolError
    worker.context_id = "invalid-context-id"
    with pytest.raises(WorkerProtocolError) as exc_info:
        await worker.await_assignment(timeout_s=0.5)
    assert "unknown context" in exc_info.value.message.lower()

    # 2. ask_alice on a task not assigned to bob raises WorkerProtocolError
    await worker.check_in()
    hub_store.check_in("charlie", ["python"])
    charlie_task = hub_store.assign_task("charlie", "implementer", "Task Charlie", "Inst")
    with pytest.raises(WorkerProtocolError) as exc_info:
        await worker.ask_alice(charlie_task.id, "Question?", timeout_s=0.5)
    assert "not assigned to bob" in exc_info.value.message.lower()


async def test_retry_on_503(
    worker_settings: WorkerSettings,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url=worker_settings.hub_url
    ) as mock_client:
        worker = WorkerHubClient(worker_settings, http_client=mock_client)
        resp = await worker._request_with_retry("GET", "/healthz")
        assert resp.status_code == 200
        assert attempts == 2
