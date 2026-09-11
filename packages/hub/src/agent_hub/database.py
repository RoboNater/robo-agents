"""SQLite schema creation for durable hub state."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from agent_hub_common import (
    UNKNOWN,
    AgentStatus,
    EventKind,
    ModelSource,
    TaskState,
    WorkflowStatus,
)

SCHEMA_VERSION = 5


class DatabaseVersionError(RuntimeError):
    """Raised when the on-disk schema does not match this application."""


def _sql_values(enum_type: type[StrEnum]) -> str:
    # Values come only from closed application enums, never from runtime input.
    return ", ".join(f"'{item.value}'" for item in enum_type)


# The worker identity profile (spec §3). Declared once so a fresh schema and a
# migrated one get identical columns.
PROFILE_COLUMNS = {
    "harness": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'",
    "harness_version": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'",
    "provider": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'",
    "model": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'",
    "model_source": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'"
    f" CHECK (model_source IN ({_sql_values(ModelSource)}))",
    "workspace_id": "TEXT",
}
_PROFILE_SQL = "".join(f"\n    {name} {spec}," for name, spec in PROFILE_COLUMNS.items())


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS workflow (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({_sql_values(WorkflowStatus)})),
    policy_json TEXT NOT NULL DEFAULT '{{}}',
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent (
    name TEXT PRIMARY KEY,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ({_sql_values(AgentStatus)})),
    context_id TEXT UNIQUE,
    last_seen TEXT NOT NULL,
    worker_instance_id TEXT NOT NULL DEFAULT '',
    last_heartbeat TEXT NOT NULL DEFAULT '',
    last_progress_at TEXT,
    current_task_id TEXT,{_PROFILE_SQL}
    FOREIGN KEY (current_task_id) REFERENCES task(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    assignee TEXT,
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ({_sql_values(TaskState)})),
    lease_expires TEXT,
    lease_duration_s REAL NOT NULL DEFAULT 1800,
    result_json TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES workflow(id) ON DELETE CASCADE,
    FOREIGN KEY (assignee) REFERENCES agent(name) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    context_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('to_alice', 'from_alice')),
    parts_json TEXT NOT NULL,
    ts TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ({_sql_values(EventKind)})),
    payload_json TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0, 1)),
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    summary TEXT NOT NULL,
    rationale TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation (
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (actor, operation_id)
);

CREATE INDEX IF NOT EXISTS idx_task_workflow_state ON task(workflow_id, state);
CREATE INDEX IF NOT EXISTS idx_task_assignee ON task(assignee);
CREATE INDEX IF NOT EXISTS idx_message_context_ts ON message(context_id, ts);
CREATE INDEX IF NOT EXISTS idx_event_inbox ON event(consumed, id);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database(path: Path) -> None:
    """Create the database and apply the initial schema or migrations."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with database(path) as connection:
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version == SCHEMA_VERSION:
            return
        if current_version == 0:
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if current_version > SCHEMA_VERSION:
            raise DatabaseVersionError(
                f"database schema version {current_version} is incompatible with "
                f"expected version {SCHEMA_VERSION}"
            )
        # Each step inspects the table rather than trusting the version number,
        # so it is safe to re-run and migrations compose across schema versions
        # (v1/v2 -> v4, and mainline v3 -> v4).
        _migrate_agent_profile(connection)
        _migrate_operation_table(connection)
        _migrate_worker_heartbeat(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_agent_profile(connection: sqlite3.Connection) -> None:
    """Replace the v2 `runtime` column with the identity profile (#26).

    A recorded runtime was the harness name, so it carries over as `harness`;
    everything the old schema never captured is `unknown`.
    """

    columns = _columns(connection, "agent")
    for name, spec in PROFILE_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE agent ADD COLUMN {name} {spec}")
    if "runtime" in columns:
        connection.execute(
            "UPDATE agent SET harness = trim(runtime) WHERE trim(coalesce(runtime, '')) != ''"
        )
        connection.execute("ALTER TABLE agent DROP COLUMN runtime")


def _migrate_operation_table(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS operation (
            actor TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created TEXT NOT NULL,
            PRIMARY KEY (actor, operation_id)
        )
    """)
    columns = _columns(connection, "operation")
    if "created" not in columns:
        connection.execute("ALTER TABLE operation ADD COLUMN created TEXT NOT NULL DEFAULT ''")


def _migrate_worker_heartbeat(connection: sqlite3.Connection) -> None:
    """Add timer-driven liveness and instance identity fields for schema v5."""

    agent_columns = _columns(connection, "agent")
    if "worker_instance_id" not in agent_columns:
        connection.execute(
            "ALTER TABLE agent ADD COLUMN worker_instance_id TEXT NOT NULL DEFAULT ''"
        )
    if "last_heartbeat" not in agent_columns:
        connection.execute(
            "ALTER TABLE agent ADD COLUMN last_heartbeat TEXT NOT NULL DEFAULT ''"
        )
        connection.execute("UPDATE agent SET last_heartbeat = last_seen")
    if "last_progress_at" not in agent_columns:
        connection.execute("ALTER TABLE agent ADD COLUMN last_progress_at TEXT")

    task_columns = _columns(connection, "task")
    if "lease_duration_s" not in task_columns:
        connection.execute(
            "ALTER TABLE task ADD COLUMN lease_duration_s REAL NOT NULL DEFAULT 1800"
        )
        # Existing leases retain their original window where SQLite can derive
        # it; terminal tasks and malformed legacy timestamps keep the default.
        connection.execute("""
            UPDATE task
            SET lease_duration_s = max(
                0,
                (julianday(lease_expires) - julianday(created)) * 86400
            )
            WHERE lease_expires IS NOT NULL
              AND julianday(lease_expires) IS NOT NULL
              AND julianday(created) IS NOT NULL
        """)


@contextmanager
def database(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a transactional connection and always close it after use."""

    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
