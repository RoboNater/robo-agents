import asyncio
import importlib.util
from pathlib import Path
import time
from typing import Any

import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import database, initialize_database
from agent_hub.mcp import create_mcp
from agent_hub.store import ConflictError, HubStore
from agent_hub_common import (
    AgentProfile,
    AgentStatus,
    EventKind,
    EventState,
    HubSettings,
    WorkflowStatus,
)
from conftest import BASE_URL, TOKEN
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings

script_path = Path(__file__).resolve().parents[1] / "scripts" / "mock-alice.py"
spec = importlib.util.spec_from_file_location("mock_alice", script_path)
assert spec is not None and spec.loader is not None
mock_alice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_alice)


def test_event_delivery_and_implicit_ack_store(tmp_path: Path) -> None:
    db_path = tmp_path / "hub.db"
    initialize_database(db_path)
    store = HubStore(db_path, default_event_lease_s=10.0)

    # 1. Enqueue event
    event = store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    assert event.state == EventState.QUEUED
    assert event.delivery_id is None
    assert event.delivery_attempts == 0
    assert store.pending_events() == 1

    state = store.get_state()
    assert state["queued_events"] == 1
    assert len(state["unacked_delivered"]) == 0

    # 2. Lease event
    leased = store.lease_next_event(lease_s=5.0)
    assert leased is not None
    assert leased.id == event.id
    assert leased.state == EventState.DELIVERED
    assert leased.delivery_attempts == 1
    assert leased.delivery_id is not None
    assert leased.delivered_at is not None
    assert leased.delivery_expires is not None
    assert store.pending_events() == 0

    state = store.get_state()
    assert state["queued_events"] == 0
    assert len(state["unacked_delivered"]) == 1
    assert state["unacked_delivered"][0]["delivery_id"] == leased.delivery_id

    # Cannot re-lease while active
    assert store.lease_next_event() is None

    # 3. Ack event
    acked = store.ack_event(leased.delivery_id)
    assert acked is True

    with database(db_path) as conn:
        row = conn.execute("SELECT * FROM event WHERE id = ?", (event.id,)).fetchone()
        assert row["state"] == "acked"
        assert row["acked_at"] is not None

    state = store.get_state()
    assert state["queued_events"] == 0
    assert len(state["unacked_delivered"]) == 0


def test_event_redelivery_after_lease_expiry(tmp_path: Path) -> None:
    db_path = tmp_path / "hub.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "alice"})

    # First lease with very short duration
    first_delivery = store.lease_next_event(lease_s=0.1)
    assert first_delivery is not None
    assert first_delivery.delivery_attempts == 1
    old_delivery_id = first_delivery.delivery_id

    # Immediate second lease returns nothing
    assert store.lease_next_event() is None

    # Wait for lease to expire
    time.sleep(0.15)

    # Redelivered!
    second_delivery = store.lease_next_event(lease_s=10.0)
    assert second_delivery is not None
    assert second_delivery.id == first_delivery.id
    assert second_delivery.delivery_attempts == 2
    assert second_delivery.delivery_id != old_delivery_id

    # Ack new delivery
    assert store.ack_event(second_delivery.delivery_id) is True
    # Can no longer lease
    assert store.lease_next_event() is None


def test_ack_wrong_or_expired_delivery_id(tmp_path: Path) -> None:
    db_path = tmp_path / "hub.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    leased = store.lease_next_event(lease_s=0.1)
    assert leased is not None
    delivery_id = leased.delivery_id

    # Ack with wrong id is ignored
    assert store.ack_event("bogus-delivery-id") is False
    with database(db_path) as conn:
        row = conn.execute("SELECT * FROM event WHERE id = ?", (leased.id,)).fetchone()
        assert row["state"] == "delivered"

    # Wait for lease to expire
    time.sleep(0.15)

    # Ack with expired delivery_id is ignored
    assert store.ack_event(delivery_id) is False
    with database(db_path) as conn:
        row = conn.execute("SELECT * FROM event WHERE id = ?", (leased.id,)).fetchone()
        assert row["state"] == "delivered"

    # Redelivery succeeds and generates fresh delivery_id
    re_leased = store.lease_next_event(lease_s=5.0)
    assert re_leased is not None
    assert re_leased.delivery_attempts == 2
    assert re_leased.delivery_id != delivery_id
    assert store.ack_event(re_leased.delivery_id) is True


def test_fifo_ordering_expired_before_newer(tmp_path: Path) -> None:
    db_path = tmp_path / "hub.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    # Enqueue 1 and 2
    e1 = store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    e2 = store.append_event(EventKind.TASK_PROGRESS, {"note": "step 1"})

    # Lease e1 with short expiry
    l1 = store.lease_next_event(lease_s=0.1)
    assert l1 is not None and l1.id == e1.id

    # Wait for e1 to expire
    time.sleep(0.15)

    # Enqueue e3
    e3 = store.append_event(EventKind.TASK_PROGRESS, {"note": "step 2"})

    # Next lease must be e1 (expired, lowest id), then e2 (queued), then e3 (queued)
    rl1 = store.lease_next_event(lease_s=5.0)
    assert rl1 is not None and rl1.id == e1.id
    assert rl1.delivery_attempts == 2
    assert store.ack_event(rl1.delivery_id) is True

    l2 = store.lease_next_event(lease_s=5.0)
    assert l2 is not None and l2.id == e2.id
    assert store.ack_event(l2.delivery_id) is True

    l3 = store.lease_next_event(lease_s=5.0)
    assert l3 is not None and l3.id == e3.id
    assert store.ack_event(l3.delivery_id) is True

    assert store.lease_next_event() is None


def test_events_survive_hub_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "hub.db"
    initialize_database(db_path)

    # Store 1 enqueues and leases an event
    store1 = HubStore(db_path)
    e1 = store1.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    store1.append_event(EventKind.TASK_PROGRESS, {"note": "started"})
    l1 = store1.lease_next_event(lease_s=0.1)
    assert l1 is not None and l1.id == e1.id

    # Hub crashes / restarts: Store 2 opens the same db
    time.sleep(0.15)
    store2 = HubStore(db_path)

    state = store2.get_state()
    assert state["queued_events"] == 1
    assert len(state["unacked_delivered"]) == 1

    # Store 2 leases next event -> gets expired e1 with attempt 2
    l1_re = store2.lease_next_event(lease_s=5.0)
    assert l1_re is not None and l1_re.id == e1.id
    assert l1_re.delivery_attempts == 2
    assert store2.ack_event(l1_re.delivery_id) is True

    # Next event is the queued task_progress
    l2 = store2.lease_next_event(lease_s=5.0)
    assert l2 is not None and l2.kind == EventKind.TASK_PROGRESS
    assert store2.ack_event(l2.delivery_id) is True


def test_state_guards_idempotency(tmp_path: Path) -> None:
    db_path = tmp_path / "hub.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    # 1. log_decision deduplication on key
    d1 = store.log_decision("Summary A", "Rationale A", key="key-1")
    d2 = store.log_decision("Summary A", "Rationale A", key="key-1")
    assert d1 == d2
    with database(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM decision WHERE key = 'key-1'").fetchone()[
            "n"
        ]
        assert count == 1

    # 2. set_workflow_status duplicate audit entries
    store.set_workflow_status(WorkflowStatus.PAUSED, "Paused work")
    store.set_workflow_status(WorkflowStatus.PAUSED, "Paused work again")
    with database(db_path) as conn:
        audit_rows = conn.execute(
            "SELECT * FROM decision WHERE rationale LIKE '%Workflow status set to paused%'"
        ).fetchall()
        assert len(audit_rows) == 1

    # 3. assign_task guard when agent is busy
    store.check_in("worker-1", AgentProfile())
    task = store.assign_task("worker-1", "implementer", "Task 1", "Instructions")
    with pytest.raises(ConflictError, match="already holds task"):
        store.assign_task("worker-1", "reviewer", "Task 2", "Instructions")

    # 4. reply guard when task not in INPUT_REQUIRED
    # task is in WORKING state; reply should be a safe no-op
    with database(db_path) as conn:
        before_count = conn.execute("SELECT COUNT(*) AS n FROM message").fetchone()["n"]
    store.reply(task.id, "Late answer")
    with database(db_path) as conn:
        after_count = conn.execute("SELECT COUNT(*) AS n FROM message").fetchone()["n"]
    assert after_count == before_count

    # 5. release_agent guard when agent is already released
    store.release_agent("worker-1")
    agent = store.agent_by_name("worker-1")
    assert agent is not None and agent.status == AgentStatus.RELEASED
    # Second release is a safe no-op
    store.release_agent("worker-1")
    agent_second = store.agent_by_name("worker-1")
    assert agent_second is not None and agent_second.status == AgentStatus.RELEASED


async def test_mcp_wait_for_event_and_implicit_ack(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)
    store = HubStore(path)
    server = create_mcp(store)

    async def call(name: str, **args: Any) -> Any:
        result = await server.call_tool(name, args)
        assert isinstance(result, tuple)
        return result[1]

    # Enqueue event
    store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})

    # Wait for event via MCP
    res1 = await call("wait_for_event", timeout_s=1.0)
    event1 = res1.get("event")
    assert event1 is not None
    assert event1["kind"] == "agent_checked_in"
    assert event1["delivery_attempts"] == 1
    deliv_id = event1["delivery_id"]
    assert deliv_id is not None

    # Check get_state via MCP
    state1 = await call("get_state")
    assert state1["queued_events"] == 0
    assert len(state1["unacked_delivered"]) == 1
    assert state1["unacked_delivered"][0]["delivery_id"] == deliv_id

    # Wait for next event while acking the previous delivery
    res2 = await call("wait_for_event", timeout_s=0.1, ack=deliv_id)
    assert res2.get("event") is None

    state2 = await call("get_state")
    assert len(state2["unacked_delivered"]) == 0


async def test_mock_alice_crash_and_recovery_scenarios(tmp_path: Path) -> None:
    db_path = tmp_path / "hub.db"
    initialize_database(db_path)
    # Configure short event lease for fast recovery tests
    store = HubStore(db_path, default_event_lease_s=0.2)

    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=db_path,
        token=TOKEN,
        token_file=tmp_path / "token",
        guides_dir=tmp_path / "guides",
        default_wait_s=0.5,
        max_wait_s=1.0,
        lost_after_s=60.0,
        sweep_interval_s=3600.0,
        event_lease_s=0.2,
    )
    app = create_app(settings)

    worker_settings = WorkerSettings(
        hub_url=BASE_URL,
        token=TOKEN,
        agent_name="bob",
        profile=AgentProfile(harness="claude-code"),
        default_wait_s=0.5,
        max_retries=2,
        backoff_factor_s=0.01,
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http_client,
    ):
        worker = WorkerHubClient(worker_settings, http_client=http_client)

        async def worker_lifecycle() -> None:
            await worker.check_in(["python"])
            assignment = await worker.await_assignment(timeout_s=5.0)
            task_id = assignment["task_id"]
            await worker.report_progress(task_id, "Working...")
            await worker.submit_result(
                task_id,
                "completed",
                "Work done",
                artifacts=[{"name": "pr", "url": "https://github.com/repo/pull/1"}],
            )
            rel = await worker.await_assignment(timeout_s=5.0)
            assert rel == {"release": True}

        # 1. Alice crashes after action (task assignment)
        worker_task = asyncio.create_task(worker_lifecycle())

        # First Alice session crashes after assigning task
        with pytest.raises(mock_alice.AliceCrashError):
            await mock_alice.drive_one_task(
                store=store,
                expected_agent="bob",
                expected_harness="claude-code",
                timeout_s=5.0,
                crash_at="after_action",
            )

        # Wait for the check-in event lease to expire
        await asyncio.sleep(0.25)

        # Second Alice session takes over, recovers active task, and completes
        res = await mock_alice.drive_one_task(
            store=store,
            expected_agent="bob",
            expected_harness="claude-code",
            timeout_s=5.0,
        )
        await worker_task

        assert res is not None
        assert store.get_state()["workflow"]["status"] == WorkflowStatus.DONE.value

        # Verify no duplicate task assignments or duplicate decisions occurred
        with database(db_path) as conn:
            tasks = conn.execute("SELECT * FROM task").fetchall()
            assert len(tasks) == 1
            decisions = conn.execute(
                "SELECT * FROM decision WHERE key = ?", (f"assign:{tasks[0]['id']}",)
            ).fetchall()
            assert len(decisions) == 1


async def test_mock_alice_crash_at_delivery_and_recovery(tmp_path: Path) -> None:
    db_path = tmp_path / "hub_delivery_crash.db"
    initialize_database(db_path)
    store = HubStore(db_path, default_event_lease_s=0.2)

    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=db_path,
        token=TOKEN,
        token_file=tmp_path / "token",
        guides_dir=tmp_path / "guides",
        default_wait_s=0.5,
        max_wait_s=1.0,
        lost_after_s=60.0,
        sweep_interval_s=3600.0,
        event_lease_s=0.2,
    )
    app = create_app(settings)

    worker_settings = WorkerSettings(
        hub_url=BASE_URL,
        token=TOKEN,
        agent_name="bob",
        profile=AgentProfile(harness="claude-code"),
        default_wait_s=0.5,
        max_retries=2,
        backoff_factor_s=0.01,
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http_client,
    ):
        worker = WorkerHubClient(worker_settings, http_client=http_client)

        async def worker_lifecycle() -> None:
            await worker.check_in(["python"])
            assignment = await worker.await_assignment(timeout_s=5.0)
            task_id = assignment["task_id"]
            await worker.report_progress(task_id, "Working...")
            await worker.submit_result(
                task_id,
                "completed",
                "Work done",
                artifacts=[{"name": "pr", "url": "https://github.com/repo/pull/1"}],
            )
            rel = await worker.await_assignment(timeout_s=5.0)
            assert rel == {"release": True}

        worker_task = asyncio.create_task(worker_lifecycle())

        # First Alice session crashes immediately when event is delivered
        with pytest.raises(mock_alice.AliceCrashError):
            await mock_alice.drive_one_task(
                store=store,
                expected_agent="bob",
                expected_harness="claude-code",
                timeout_s=5.0,
                crash_at="delivery",
            )

        # Wait for the check-in event lease to expire
        await asyncio.sleep(0.25)

        # Second Alice session takes over, receives redelivery, and completes
        res = await mock_alice.drive_one_task(
            store=store,
            expected_agent="bob",
            expected_harness="claude-code",
            timeout_s=5.0,
        )
        await worker_task

        assert res is not None
        assert store.get_state()["workflow"]["status"] == WorkflowStatus.DONE.value

