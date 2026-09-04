"""Durable hub state and the blocking reads layered on top of it.

Every worker intent in spec §4.1 and every Alice action in §4.2 that a worker
can observe lands here. The A2A wire format stays in `protocol`; this module
deals only in records and in the wakeups that release a held request.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from sqlite3 import Connection, Row
from time import monotonic
from typing import Any, TypeVar
from uuid import uuid4

from agent_hub_common import (
    AgentStatus,
    EventKind,
    TaskState,
    WorkflowStatus,
    to_iso,
    utcnow,
    utcnow_iso,
)

from .database import database
from .signals import EVENT_KEY, Signals, context_key, task_key

DEFAULT_GOAL = "Drive the assigned GitHub issue to a merged pull request."
DEFAULT_LEASE_MIN = 30.0
LOST_REASON = "lost"

TERMINAL_STATES = (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED)
OPEN_STATES = (TaskState.SUBMITTED, TaskState.WORKING, TaskState.INPUT_REQUIRED)

T = TypeVar("T")


class StoreError(RuntimeError):
    """Base class for refusals that the protocol layer maps onto A2A errors."""


class NotFoundError(StoreError):
    """Raised when an addressed agent or task does not exist."""


class ConflictError(StoreError):
    """Raised when an operation contradicts the current state."""


@dataclass(frozen=True, slots=True)
class AgentRecord:
    name: str
    capabilities: list[str]
    status: AgentStatus
    context_id: str
    last_seen: str
    current_task_id: str | None


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    workflow_id: str
    assignee: str | None
    role: str
    title: str
    instructions: str
    state: TaskState
    lease_expires: str | None
    result: dict[str, Any] | None
    created: str
    updated: str


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: int
    task_id: str | None
    context_id: str
    sender: str
    direction: str
    parts: list[dict[str, Any]]
    ts: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    kind: EventKind
    payload: dict[str, Any]
    ts: str


@dataclass(frozen=True, slots=True)
class Released:
    """Sentinel returned instead of a task when Alice has released the agent."""

    agent: str


def _json_object(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    loaded: Any = json.loads(raw)
    return loaded if isinstance(loaded, dict) else None


def _agent(row: Row) -> AgentRecord:
    capabilities: Any = json.loads(row["capabilities_json"])
    return AgentRecord(
        name=row["name"],
        capabilities=[str(item) for item in capabilities] if isinstance(capabilities, list) else [],
        status=AgentStatus(row["status"]),
        context_id=row["context_id"],
        last_seen=row["last_seen"],
        current_task_id=row["current_task_id"],
    )


def _task(row: Row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        workflow_id=row["workflow_id"],
        assignee=row["assignee"],
        role=row["role"],
        title=row["title"],
        instructions=row["instructions"],
        state=TaskState(row["state"]),
        lease_expires=row["lease_expires"],
        result=_json_object(row["result_json"]),
        created=row["created"],
        updated=row["updated"],
    )


def _message(row: Row) -> MessageRecord:
    parts: Any = json.loads(row["parts_json"])
    return MessageRecord(
        id=row["id"],
        task_id=row["task_id"],
        context_id=row["context_id"],
        sender=row["sender"],
        direction=row["direction"],
        parts=[part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else [],
        ts=row["ts"],
    )


def _event(row: Row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        kind=EventKind(row["kind"]),
        payload=_json_object(row["payload_json"]) or {},
        ts=row["ts"],
    )


def text_part(text: str, **metadata: Any) -> dict[str, Any]:
    """Build the A2A text part shape the transcript stores."""

    part: dict[str, Any] = {"kind": "text", "text": text}
    if metadata:
        part["metadata"] = metadata
    return part


@dataclass(slots=True)
class HubStore:
    """SQLite-backed hub state, plus the waits that hold a worker's request."""

    path: Path
    signals: Signals = field(default_factory=Signals)

    # -- workflow -----------------------------------------------------------

    def ensure_workflow(
        self, goal: str = DEFAULT_GOAL, policy: Mapping[str, Any] | None = None
    ) -> str:
        """Return the id of the single PoC workflow, creating it if needed."""

        with database(self.path) as connection:
            return self._ensure_workflow(connection, goal, policy)

    def _ensure_workflow(
        self, connection: Connection, goal: str, policy: Mapping[str, Any] | None
    ) -> str:
        row = connection.execute("SELECT id FROM workflow ORDER BY created LIMIT 1").fetchone()
        if row is not None:
            return str(row["id"])
        workflow_id = uuid4().hex
        connection.execute(
            "INSERT INTO workflow (id, goal, status, policy_json, created) VALUES (?, ?, ?, ?, ?)",
            (
                workflow_id,
                goal,
                WorkflowStatus.ACTIVE.value,
                json.dumps(dict(policy or {})),
                utcnow_iso(),
            ),
        )
        return workflow_id

    # -- agents -------------------------------------------------------------

    def check_in(
        self,
        name: str,
        capabilities: Sequence[str],
        runtime: str | None = None,
    ) -> AgentRecord:
        """Register a worker, or re-admit a returning one on its own context."""

        now = utcnow_iso()
        with database(self.path) as connection:
            row = connection.execute("SELECT * FROM agent WHERE name = ?", (name,)).fetchone()
            if row is None:
                context_id = uuid4().hex
                connection.execute(
                    "INSERT INTO agent (name, capabilities_json, status, context_id, last_seen)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (name, json.dumps(list(capabilities)), AgentStatus.IDLE.value, context_id, now),
                )
            else:
                # A returning worker keeps its context id so Alice reads one
                # unbroken thread per agent across restarts.
                context_id = row["context_id"]
                current = self._open_task_id(connection, row["current_task_id"])
                connection.execute(
                    "UPDATE agent SET capabilities_json = ?, status = ?, last_seen = ?,"
                    " current_task_id = ? WHERE name = ?",
                    (
                        json.dumps(list(capabilities)),
                        _readmitted(AgentStatus(row["status"]), current).value,
                        now,
                        current,
                        name,
                    ),
                )
            self._add_message(
                connection,
                task_id=None,
                context_id=context_id,
                sender=name,
                direction="to_alice",
                parts=[text_part("READY", kind="check_in", runtime=runtime)],
            )
            self._add_event(
                connection,
                EventKind.AGENT_CHECKED_IN,
                {
                    "agent": name,
                    "capabilities": list(capabilities),
                    "runtime": runtime,
                    "context_id": context_id,
                },
            )
            agent = self._require_agent(connection, name)
        self.signals.notify(EVENT_KEY)
        return agent

    def _open_task_id(self, connection: Connection, task_id: str | None) -> str | None:
        """Return `task_id` only while that task is still open."""

        if task_id is None:
            return None
        row = connection.execute("SELECT state FROM task WHERE id = ?", (task_id,)).fetchone()
        if row is None or TaskState(row["state"]) in TERMINAL_STATES:
            return None
        return task_id

    def agent_by_name(self, name: str) -> AgentRecord | None:
        with database(self.path) as connection:
            row = connection.execute("SELECT * FROM agent WHERE name = ?", (name,)).fetchone()
        return None if row is None else _agent(row)

    def agent_by_context(self, context_id: str) -> AgentRecord | None:
        with database(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM agent WHERE context_id = ?", (context_id,)
            ).fetchone()
        return None if row is None else _agent(row)

    def agents(self) -> list[AgentRecord]:
        with database(self.path) as connection:
            rows = connection.execute("SELECT * FROM agent ORDER BY name").fetchall()
        return [_agent(row) for row in rows]

    def touch(self, name: str) -> None:
        """Record contact from a worker; every call is its own heartbeat."""

        with database(self.path) as connection:
            connection.execute(
                "UPDATE agent SET last_seen = ? WHERE name = ?", (utcnow_iso(), name)
            )

    def release_agent(self, name: str) -> AgentRecord:
        """Mark an agent released so its next assignment wait returns release."""

        with database(self.path) as connection:
            agent = self._require_agent(connection, name)
            connection.execute(
                "UPDATE agent SET status = ?, last_seen = ? WHERE name = ?",
                (AgentStatus.RELEASED.value, utcnow_iso(), name),
            )
            released = self._require_agent(connection, name)
        self.signals.notify(context_key(agent.context_id))
        return released

    def _require_agent(self, connection: Connection, name: str) -> AgentRecord:
        row = connection.execute("SELECT * FROM agent WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise NotFoundError(f"unknown agent: {name}")
        return _agent(row)

    # -- tasks --------------------------------------------------------------

    def assign_task(
        self,
        agent: str,
        role: str,
        title: str,
        instructions: str,
        lease_min: float = DEFAULT_LEASE_MIN,
    ) -> TaskRecord:
        """Create a task for an idle agent and unblock its pending wait."""

        now = utcnow_iso()
        with database(self.path) as connection:
            record = self._require_agent(connection, agent)
            if record.status is not AgentStatus.IDLE:
                held = self._open_task_id(connection, record.current_task_id)
                if held is not None:
                    raise ConflictError(f"agent {agent} already holds task {held}")
                # A released or lost worker is not there to claim the task, and
                # the sweeper will not report it again. Check-in is the only
                # re-admission: it is the one call that proves a worker is back.
                raise ConflictError(
                    f"agent {agent} is {record.status.value}, not idle; "
                    "it must check in again before it can be given work"
                )
            workflow_id = self._ensure_workflow(connection, DEFAULT_GOAL, None)
            task_id = uuid4().hex
            connection.execute(
                "INSERT INTO task (id, workflow_id, assignee, role, title, instructions, state,"
                " lease_expires, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    workflow_id,
                    agent,
                    role,
                    title,
                    instructions,
                    TaskState.SUBMITTED.value,
                    to_iso(utcnow() + timedelta(minutes=lease_min)),
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE agent SET status = ?, current_task_id = ? WHERE name = ?",
                (AgentStatus.BUSY.value, task_id, agent),
            )
            self._add_message(
                connection,
                task_id=task_id,
                context_id=record.context_id,
                sender="alice",
                direction="from_alice",
                parts=[text_part(instructions, kind="assignment", role=role, title=title)],
            )
            task = self._require_task(connection, task_id)
        self.signals.notify(context_key(record.context_id))
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with database(self.path) as connection:
            row = connection.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else _task(row)

    def task_context_id(self, task_id: str) -> str:
        """Return the context of the agent a task is assigned to, if any."""

        with database(self.path) as connection:
            return self._task_context_id(connection, self._require_task(connection, task_id))

    def tasks(self) -> list[TaskRecord]:
        with database(self.path) as connection:
            rows = connection.execute("SELECT * FROM task ORDER BY created").fetchall()
        return [_task(row) for row in rows]

    def task_history(self, task_id: str, limit: int | None = None) -> list[MessageRecord]:
        """Return a task's transcript, newest-last, optionally trimmed."""

        with database(self.path) as connection:
            rows = connection.execute(
                "SELECT * FROM message WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        history = [_message(row) for row in rows]
        if limit is not None and limit >= 0:
            history = history[len(history) - limit :] if limit else []
        return history

    def record_progress(self, task_id: str, agent: str, note: str) -> None:
        """Record a fire-and-forget progress note and queue it for Alice."""

        with database(self.path) as connection:
            task = self._require_open_task(connection, task_id)
            record = self._require_agent(connection, agent)
            self._add_message(
                connection,
                task_id=task.id,
                context_id=record.context_id,
                sender=agent,
                direction="to_alice",
                parts=[text_part(note, kind="progress")],
            )
            self._add_event(
                connection,
                EventKind.TASK_PROGRESS,
                {"task_id": task.id, "agent": agent, "note": note},
            )
            self._touch(connection, agent)
        self.signals.notify(EVENT_KEY)

    def open_question(self, task_id: str, agent: str, question: str, sent_as: str) -> int:
        """Park a task on `input-required` and return the question's row id.

        `sent_as` is the caller's own message id, and a retry after a timeout
        carries the one it first asked under. That is what makes "call again"
        safe: Alice may have answered in the gap between attempts, and her
        answer is older than a second question would be, so a retry that opened
        a new question could never see it. A recognised retry resumes the
        original instead — no second question, no duplicate event for Alice.
        """

        with database(self.path) as connection:
            task = self._require_open_task(connection, task_id)
            record = self._require_agent(connection, agent)
            asked = self._asked_question(connection, task.id, sent_as)
            if asked is not None:
                self._touch(connection, agent)
                return asked
            message_id = self._add_message(
                connection,
                task_id=task.id,
                context_id=record.context_id,
                sender=agent,
                direction="to_alice",
                parts=[text_part(question, kind="question", message_id=sent_as)],
            )
            self._set_state(connection, task.id, TaskState.INPUT_REQUIRED)
            self._add_event(
                connection,
                EventKind.WORKER_QUESTION,
                {
                    "task_id": task.id,
                    "agent": agent,
                    "question": question,
                    "message_id": message_id,
                },
            )
            self._touch(connection, agent)
        self.signals.notify(EVENT_KEY)
        return message_id

    def _asked_question(self, connection: Connection, task_id: str, sent_as: str) -> int | None:
        """Return the row id of a question already asked under `sent_as`."""

        if not sent_as:
            return None
        rows = connection.execute(
            "SELECT id, parts_json FROM message WHERE task_id = ? AND direction = 'to_alice'"
            " ORDER BY id",
            (task_id,),
        ).fetchall()
        for row in rows:
            for part in json.loads(row["parts_json"]):
                if not isinstance(part, dict):
                    continue
                metadata = part.get("metadata") or {}
                if metadata.get("kind") == "question" and metadata.get("message_id") == sent_as:
                    return int(row["id"])
        return None

    def reply(self, task_id: str, text: str) -> None:
        """Answer a worker question and put the task back to `working`."""

        with database(self.path) as connection:
            task = self._require_open_task(connection, task_id)
            context_id = self._task_context_id(connection, task)
            self._add_message(
                connection,
                task_id=task.id,
                context_id=context_id,
                sender="alice",
                direction="from_alice",
                parts=[text_part(text, kind="reply")],
            )
            self._set_state(connection, task.id, TaskState.WORKING)
        self.signals.notify(task_key(task_id))

    def pending_reply(self, task_id: str, after_message_id: int) -> MessageRecord | None:
        """Return Alice's first reply on this task after the given message."""

        with database(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM message WHERE task_id = ? AND direction = 'from_alice'"
                " AND id > ? ORDER BY id LIMIT 1",
                (task_id, after_message_id),
            ).fetchone()
        return None if row is None else _message(row)

    def submit_result(
        self,
        task_id: str,
        agent: str,
        status: TaskState,
        summary: str,
        artifacts: Sequence[Mapping[str, Any]] = (),
    ) -> TaskRecord:
        """Drive a task to a terminal state and free its worker."""

        if status not in (TaskState.COMPLETED, TaskState.FAILED):
            raise ConflictError(f"a result must be completed or failed, got {status.value}")
        payload: dict[str, Any] = {
            "status": status.value,
            "summary": summary,
            "artifacts": [dict(artifact) for artifact in artifacts],
        }
        with database(self.path) as connection:
            task = self._require_open_task(connection, task_id)
            record = self._require_agent(connection, agent)
            self._add_message(
                connection,
                task_id=task.id,
                context_id=record.context_id,
                sender=agent,
                direction="to_alice",
                parts=[text_part(summary, kind="result", status=status.value)],
            )
            self._finish(connection, task.id, status, payload)
            self._add_event(
                connection,
                EventKind.TASK_COMPLETED
                if status is TaskState.COMPLETED
                else EventKind.TASK_FAILED,
                {"task_id": task.id, "agent": agent} | payload,
            )
            self._touch(connection, agent)
            finished = self._require_task(connection, task.id)
        self.signals.notify(EVENT_KEY)
        return finished

    def cancel_task(self, task_id: str) -> TaskRecord:
        """Cancel an open task and release whoever was holding it."""

        with database(self.path) as connection:
            row = connection.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"unknown task: {task_id}")
            task = _task(row)
            if task.state in TERMINAL_STATES:
                raise ConflictError(f"task {task_id} is already {task.state.value}")
            context_id = self._task_context_id(connection, task)
            self._finish(connection, task.id, TaskState.CANCELED, None)
            canceled = self._require_task(connection, task.id)
        self.signals.notify(task_key(task_id))
        if context_id:
            self.signals.notify(context_key(context_id))
        return canceled

    def _require_task(self, connection: Connection, task_id: str) -> TaskRecord:
        row = connection.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"unknown task: {task_id}")
        return _task(row)

    def _require_open_task(self, connection: Connection, task_id: str) -> TaskRecord:
        task = self._require_task(connection, task_id)
        if task.state in TERMINAL_STATES:
            raise ConflictError(f"task {task_id} is already {task.state.value}")
        return task

    def _task_context_id(self, connection: Connection, task: TaskRecord) -> str:
        if task.assignee is None:
            return ""
        return self._require_agent(connection, task.assignee).context_id

    def _set_state(self, connection: Connection, task_id: str, state: TaskState) -> None:
        connection.execute(
            "UPDATE task SET state = ?, updated = ? WHERE id = ?",
            (state.value, utcnow_iso(), task_id),
        )

    def _finish(
        self,
        connection: Connection,
        task_id: str,
        state: TaskState,
        payload: dict[str, Any] | None,
    ) -> None:
        """Apply a terminal state: clear the lease and free the assignee."""

        connection.execute(
            "UPDATE task SET state = ?, updated = ?, lease_expires = NULL, result_json = ?"
            " WHERE id = ?",
            (state.value, utcnow_iso(), None if payload is None else json.dumps(payload), task_id),
        )
        connection.execute(
            "UPDATE agent SET status = CASE status WHEN ? THEN ? ELSE status END,"
            " current_task_id = NULL WHERE current_task_id = ?",
            (AgentStatus.BUSY.value, AgentStatus.IDLE.value, task_id),
        )

    def _touch(self, connection: Connection, name: str) -> None:
        connection.execute("UPDATE agent SET last_seen = ? WHERE name = ?", (utcnow_iso(), name))

    # -- transcript and events ---------------------------------------------

    def _add_message(
        self,
        connection: Connection,
        *,
        task_id: str | None,
        context_id: str,
        sender: str,
        direction: str,
        parts: list[dict[str, Any]],
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO message (task_id, context_id, sender, direction, parts_json, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, context_id, sender, direction, json.dumps(parts), utcnow_iso()),
        )
        return int(cursor.lastrowid or 0)

    def _add_event(
        self, connection: Connection, kind: EventKind, payload: Mapping[str, Any]
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO event (kind, payload_json, ts) VALUES (?, ?, ?)",
            (kind.value, json.dumps(dict(payload)), utcnow_iso()),
        )
        return int(cursor.lastrowid or 0)

    def append_event(self, kind: EventKind, payload: Mapping[str, Any]) -> EventRecord:
        """Queue an event for Alice and wake her inbox."""

        with database(self.path) as connection:
            event_id = self._add_event(connection, kind, payload)
            row = connection.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
            event = _event(row)
        self.signals.notify(EVENT_KEY)
        return event

    def next_event(self) -> EventRecord | None:
        """Consume the oldest unconsumed event, if any."""

        with database(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM event WHERE consumed = 0 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE event SET consumed = 1 WHERE id = ?", (row["id"],))
        return _event(row)

    def pending_events(self) -> int:
        with database(self.path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM event WHERE consumed = 0"
            ).fetchone()
        return int(row["n"])

    # -- waits --------------------------------------------------------------

    async def _wait_for(self, key: str, poll: Callable[[], T | None], timeout_s: float) -> T | None:
        """Poll under a subscription until `poll` yields or the deadline passes."""

        deadline = monotonic() + timeout_s
        with self.signals.subscribe(key) as woken:
            while True:
                # Clearing before the read means a notification racing the read
                # is still pending when the wait begins, instead of being lost.
                woken.clear()
                found = poll()
                if found is not None:
                    return found
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                with suppress(TimeoutError):
                    await asyncio.wait_for(woken.wait(), remaining)

    async def await_assignment(
        self, context_id: str, timeout_s: float
    ) -> TaskRecord | Released | None:
        """Hold until this agent has a task or is released; None on timeout."""

        agent = self.agent_by_context(context_id)
        if agent is None:
            raise NotFoundError(f"unknown context: {context_id}")
        self.touch(agent.name)
        try:
            return await self._wait_for(
                context_key(context_id),
                lambda: self._claim_assignment(agent.name),
                timeout_s,
            )
        finally:
            self.touch(agent.name)

    def _claim_assignment(self, name: str) -> TaskRecord | Released | None:
        with database(self.path) as connection:
            agent = self._require_agent(connection, name)
            if agent.status is AgentStatus.RELEASED:
                return Released(agent=name)
            row = connection.execute(
                "SELECT * FROM task WHERE assignee = ? AND state = ? ORDER BY created LIMIT 1",
                (name, TaskState.SUBMITTED.value),
            ).fetchone()
            if row is None:
                return None
            self._set_state(connection, row["id"], TaskState.WORKING)
            return self._require_task(connection, row["id"])

    async def await_reply(
        self, task_id: str, after_message_id: int, timeout_s: float
    ) -> MessageRecord | None:
        """Hold until Alice answers the question at `after_message_id`."""

        return await self._wait_for(
            task_key(task_id),
            lambda: self.pending_reply(task_id, after_message_id),
            timeout_s,
        )

    async def wait_for_event(self, timeout_s: float) -> EventRecord | None:
        """Hold until Alice's inbox has an event, consuming it."""

        return await self._wait_for(EVENT_KEY, self.next_event, timeout_s)

    # -- sweeper ------------------------------------------------------------

    def sweep(self, heartbeat_timeout_s: float) -> list[EventRecord]:
        """Expire overdue leases and declare silent agents lost.

        Returns the events it queued, so the caller can log what changed.
        """

        now = utcnow()
        emitted: list[int] = []
        with database(self.path) as connection:
            emitted += self._expire_leases(connection, to_iso(now))
            cutoff = to_iso(now - timedelta(seconds=heartbeat_timeout_s))
            emitted += self._lose_agents(connection, cutoff)
            if not emitted:
                return []
            events = [
                _event(row)
                for row in connection.execute(
                    f"SELECT * FROM event WHERE id IN ({_placeholders(emitted)}) ORDER BY id",
                    emitted,
                ).fetchall()
            ]
        self.signals.notify(EVENT_KEY)
        return events

    def _expire_leases(self, connection: Connection, now: str) -> list[int]:
        rows = connection.execute(
            "SELECT id, assignee, lease_expires FROM task"
            f" WHERE state IN ({_placeholders(OPEN_STATES)})"
            " AND lease_expires IS NOT NULL AND lease_expires <= ?",
            (*(state.value for state in OPEN_STATES), now),
        ).fetchall()
        emitted = []
        for row in rows:
            # The lease is cleared as it is reported, so one overdue task
            # produces one event however often the sweeper runs. Alice decides
            # whether to extend, reassign or fail it.
            connection.execute(
                "UPDATE task SET lease_expires = NULL, updated = ? WHERE id = ?",
                (utcnow_iso(), row["id"]),
            )
            emitted.append(
                self._add_event(
                    connection,
                    EventKind.LEASE_EXPIRED,
                    {
                        "task_id": row["id"],
                        "agent": row["assignee"],
                        "lease_expires": row["lease_expires"],
                    },
                )
            )
        return emitted

    def _lose_agents(self, connection: Connection, cutoff: str) -> list[int]:
        live = (AgentStatus.IDLE, AgentStatus.BUSY)
        rows = connection.execute(
            f"SELECT * FROM agent WHERE status IN ({_placeholders(live)}) AND last_seen <= ?",
            (*(status.value for status in live), cutoff),
        ).fetchall()
        emitted = []
        for row in rows:
            agent = _agent(row)
            connection.execute(
                "UPDATE agent SET status = ? WHERE name = ?",
                (AgentStatus.LOST.value, agent.name),
            )
            task_id = self._open_task_id(connection, agent.current_task_id)
            if task_id is not None:
                # Re-queue the work as failed rather than silently stranding it;
                # reassignment is Alice's call (§4.3).
                self._finish(
                    connection,
                    task_id,
                    TaskState.FAILED,
                    {
                        "status": TaskState.FAILED.value,
                        "summary": f"worker {agent.name} stopped contacting the hub",
                        "reason": LOST_REASON,
                        "artifacts": [],
                    },
                )
            emitted.append(
                self._add_event(
                    connection,
                    EventKind.AGENT_LOST,
                    {"agent": agent.name, "task_id": task_id, "last_seen": agent.last_seen},
                )
            )
        return emitted


def _readmitted(previous: AgentStatus, open_task: str | None) -> AgentStatus:
    """Status for a worker that has just re-announced itself with READY.

    A release outlives the connection it was issued on. Alice ends the workflow
    by releasing her workers, so one that restarts afterwards has to be told to
    stop; resetting it to idle would leave it polling for work nobody is left to
    assign. Every other prior status — including `lost` — is what check-in
    exists to clear.
    """

    if previous is AgentStatus.RELEASED:
        return AgentStatus.RELEASED
    return AgentStatus.BUSY if open_task else AgentStatus.IDLE


def _placeholders(values: Sequence[object]) -> str:
    return ", ".join("?" * len(values))
