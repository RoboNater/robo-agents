"""SQLite schema creation for durable hub state."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'done', 'escalated')),
    policy_json TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent (
    name TEXT PRIMARY KEY,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('idle', 'busy', 'released', 'lost')),
    context_id TEXT UNIQUE,
    last_seen TEXT NOT NULL,
    current_task_id TEXT,
    FOREIGN KEY (current_task_id) REFERENCES task(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    assignee TEXT,
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('submitted', 'working', 'input-required', 'completed', 'failed', 'canceled')
    ),
    lease_expires TEXT,
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
    kind TEXT NOT NULL CHECK (
        kind IN (
            'agent_checked_in', 'task_progress', 'task_completed', 'task_failed',
            'worker_question', 'lease_expired', 'agent_lost'
        )
    ),
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
    """Create the database and apply the initial idempotent schema."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def database(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection and close it after use."""

    connection = connect(path)
    try:
        yield connection
    finally:
        connection.close()
