import asyncio

import pytest
from agent_hub.store import (
    ConflictError,
    HubStore,
    NotFoundError,
    Released,
)
from agent_hub_common import AgentStatus, EventKind, TaskState, iso_after, to_iso, utcnow


def assign(store: HubStore, agent: str = "bob", role: str = "implementer") -> str:
    return store.assign_task(agent, role, "Fix #1", "Open a PR", lease_min=30).id


def test_check_in_registers_an_agent_and_queues_the_event(store: HubStore) -> None:
    agent = store.check_in("bob", ["python"], runtime="claude-code")

    event = store.next_event()

    assert agent.status is AgentStatus.IDLE
    assert agent.context_id
    assert agent.capabilities == ["python"]
    assert event is not None
    assert event.kind is EventKind.AGENT_CHECKED_IN
    assert event.payload["runtime"] == "claude-code"


def test_returning_worker_keeps_its_context_and_drops_a_finished_task(store: HubStore) -> None:
    first = store.check_in("bob", ["python"])
    task_id = assign(store)
    store.submit_result(task_id, "bob", TaskState.COMPLETED, "done")

    second = store.check_in("bob", ["python", "docs"])

    assert second.context_id == first.context_id
    assert second.status is AgentStatus.IDLE
    assert second.current_task_id is None
    assert second.capabilities == ["python", "docs"]


def test_returning_worker_stays_busy_while_its_task_is_open(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = assign(store)

    returned = store.check_in("bob", [])

    assert returned.status is AgentStatus.BUSY
    assert returned.current_task_id == task_id


def test_assignment_marks_the_agent_busy_and_records_the_instructions(store: HubStore) -> None:
    store.check_in("bob", [])

    task = store.assign_task("bob", "implementer", "Fix #1", "Open a PR")

    agent = store.agent_by_name("bob")
    assert task.state is TaskState.SUBMITTED
    assert task.lease_expires is not None
    assert agent is not None
    assert agent.status is AgentStatus.BUSY
    assert agent.current_task_id == task.id
    assert [part["text"] for part in store.task_history(task.id)[0].parts] == ["Open a PR"]


def test_a_second_assignment_to_a_busy_agent_is_refused(store: HubStore) -> None:
    store.check_in("bob", [])
    assign(store)

    with pytest.raises(ConflictError, match="already holds"):
        assign(store)


def test_a_released_agent_takes_no_further_assignment(store: HubStore) -> None:
    store.check_in("bob", [])
    store.release_agent("bob")

    with pytest.raises(ConflictError, match="released"):
        assign(store)


def test_assigning_to_an_unknown_agent_is_refused(store: HubStore) -> None:
    with pytest.raises(NotFoundError):
        assign(store)


async def test_waiting_worker_gets_the_task_and_claims_it(store: HubStore) -> None:
    agent = store.check_in("bob", [])

    async def alice() -> None:
        await asyncio.sleep(0.01)
        assign(store)

    waited, _ = await asyncio.gather(
        store.await_assignment(agent.context_id, 2.0), alice()
    )

    assert not isinstance(waited, Released)
    assert waited is not None
    # The task is claimed as it is handed over, so a second waiter cannot take it.
    assert waited.state is TaskState.WORKING
    assert await store.await_assignment(agent.context_id, 0.05) is None


async def test_waiting_worker_is_released_while_it_waits(store: HubStore) -> None:
    agent = store.check_in("bob", [])

    async def alice() -> None:
        await asyncio.sleep(0.01)
        store.release_agent("bob")

    waited, _ = await asyncio.gather(
        store.await_assignment(agent.context_id, 2.0), alice()
    )

    assert waited == Released(agent="bob")


async def test_assignment_wait_times_out_without_work(store: HubStore) -> None:
    agent = store.check_in("bob", [])

    assert await store.await_assignment(agent.context_id, 0.05) is None


async def test_assignment_wait_rejects_an_unknown_context(store: HubStore) -> None:
    with pytest.raises(NotFoundError):
        await store.await_assignment("nope", 0.05)


def test_progress_is_recorded_as_an_event(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = assign(store)
    store.next_event()

    store.record_progress(task_id, "bob", "branch pushed")

    event = store.next_event()
    assert event is not None
    assert event.kind is EventKind.TASK_PROGRESS
    assert event.payload == {"task_id": task_id, "agent": "bob", "note": "branch pushed"}


async def test_a_question_parks_the_task_until_alice_replies(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = assign(store)

    question_id = store.open_question(task_id, "bob", "Which base branch?")
    parked = store.get_task(task_id)

    async def alice() -> None:
        await asyncio.sleep(0.01)
        store.reply(task_id, "main")

    reply, _ = await asyncio.gather(store.await_reply(task_id, question_id, 2.0), alice())

    resumed = store.get_task(task_id)
    assert parked is not None and parked.state is TaskState.INPUT_REQUIRED
    assert reply is not None
    assert [part["text"] for part in reply.parts] == ["main"]
    assert resumed is not None and resumed.state is TaskState.WORKING


async def test_an_unanswered_question_times_out(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = assign(store)
    question_id = store.open_question(task_id, "bob", "Which base branch?")

    assert await store.await_reply(task_id, question_id, 0.05) is None
    # The question stays parked, so calling again resumes the same wait.
    parked = store.get_task(task_id)
    assert parked is not None and parked.state is TaskState.INPUT_REQUIRED


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (TaskState.COMPLETED, EventKind.TASK_COMPLETED),
        (TaskState.FAILED, EventKind.TASK_FAILED),
    ],
)
def test_a_result_ends_the_task_and_frees_the_worker(
    store: HubStore, status: TaskState, kind: EventKind
) -> None:
    store.check_in("bob", [])
    task_id = assign(store)
    while store.next_event():
        pass

    finished = store.submit_result(
        task_id, "bob", status, "PR open", [{"name": "pr", "url": "http://pr/1"}]
    )

    agent = store.agent_by_name("bob")
    event = store.next_event()
    assert finished.state is status
    assert finished.lease_expires is None
    assert finished.result is not None
    assert finished.result["artifacts"] == [{"name": "pr", "url": "http://pr/1"}]
    assert agent is not None
    assert agent.status is AgentStatus.IDLE
    assert agent.current_task_id is None
    assert event is not None and event.kind is kind


def test_a_result_must_be_terminal_and_can_only_be_reported_once(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = assign(store)

    with pytest.raises(ConflictError, match="completed or failed"):
        store.submit_result(task_id, "bob", TaskState.WORKING, "still going")

    store.submit_result(task_id, "bob", TaskState.COMPLETED, "done")
    with pytest.raises(ConflictError, match="already completed"):
        store.submit_result(task_id, "bob", TaskState.COMPLETED, "done twice")


def test_cancelling_frees_the_worker_and_only_works_once(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = assign(store)

    canceled = store.cancel_task(task_id)

    agent = store.agent_by_name("bob")
    assert canceled.state is TaskState.CANCELED
    assert agent is not None and agent.status is AgentStatus.IDLE
    with pytest.raises(ConflictError):
        store.cancel_task(task_id)


def test_events_are_consumed_once_and_in_order(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = assign(store)
    store.record_progress(task_id, "bob", "one")

    kinds = [event.kind for event in iter(store.next_event, None)]

    assert kinds == [EventKind.AGENT_CHECKED_IN, EventKind.TASK_PROGRESS]
    assert store.pending_events() == 0
    assert store.next_event() is None


async def test_waiting_for_an_event_returns_as_soon_as_one_is_queued(store: HubStore) -> None:
    async def worker() -> None:
        await asyncio.sleep(0.01)
        store.check_in("bob", [])

    event, _ = await asyncio.gather(store.wait_for_event(2.0), worker())

    assert event is not None and event.kind is EventKind.AGENT_CHECKED_IN


async def test_waiting_for_an_event_times_out_on_an_empty_inbox(store: HubStore) -> None:
    assert await store.wait_for_event(0.05) is None


def test_history_can_be_trimmed_to_the_most_recent_messages(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = assign(store)
    store.record_progress(task_id, "bob", "one")
    store.record_progress(task_id, "bob", "two")

    assert len(store.task_history(task_id)) == 3
    assert [part["text"] for part in store.task_history(task_id, 1)[0].parts] == ["two"]
    assert store.task_history(task_id, 0) == []


def test_an_overdue_lease_is_reported_exactly_once(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = store.assign_task("bob", "implementer", "Fix #1", "Open a PR", lease_min=-1).id

    first = store.sweep(heartbeat_timeout_s=3600)
    second = store.sweep(heartbeat_timeout_s=3600)

    assert [event.kind for event in first] == [EventKind.LEASE_EXPIRED]
    assert first[0].payload["task_id"] == task_id
    assert second == []
    # The task itself is untouched: whether to extend or reassign is Alice's call.
    task = store.get_task(task_id)
    assert task is not None and task.state is TaskState.SUBMITTED


def test_a_silent_worker_is_lost_and_its_task_fails(store: HubStore) -> None:
    store.check_in("bob", [])
    task_id = assign(store)
    store.sweep(heartbeat_timeout_s=-1)

    agent = store.agent_by_name("bob")
    task = store.get_task(task_id)
    assert agent is not None and agent.status is AgentStatus.LOST
    assert task is not None and task.state is TaskState.FAILED
    assert task.result is not None and task.result["reason"] == "lost"
    assert store.sweep(heartbeat_timeout_s=-1) == []


def test_a_released_worker_is_never_declared_lost(store: HubStore) -> None:
    store.check_in("bob", [])
    store.release_agent("bob")

    assert store.sweep(heartbeat_timeout_s=-1) == []


def test_a_worker_that_keeps_calling_stays_live(store: HubStore) -> None:
    store.check_in("bob", [])
    store.touch("bob")

    assert store.sweep(heartbeat_timeout_s=60) == []


def test_the_single_workflow_is_created_once(store: HubStore) -> None:
    first = store.ensure_workflow()
    store.check_in("bob", [])
    task_id = assign(store)

    task = store.get_task(task_id)
    assert store.ensure_workflow() == first
    assert task is not None and task.workflow_id == first


def test_timestamps_stay_comparable_against_stored_leases(store: HubStore) -> None:
    # Leases are compared lexically in SQL, so both sides must share a format.
    assert to_iso(utcnow()) < iso_after(60)
