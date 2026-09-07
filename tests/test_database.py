import sqlite3
from pathlib import Path

import pytest
from agent_hub.database import (
    SCHEMA_VERSION,
    DatabaseVersionError,
    database,
    initialize_database,
)


def test_initialization_creates_complete_schema_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state" / "hub.db"

    initialize_database(path)
    initialize_database(path)

    with database(path) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]

    assert {"workflow", "agent", "task", "message", "event", "decision"} <= tables
    assert version == SCHEMA_VERSION
    assert foreign_keys == 1
    assert not path.with_name(f"{path.name}-wal").exists()


def test_initialization_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(DatabaseVersionError, match="version 99"):
        initialize_database(path)


def test_migration_from_v1_adds_runtime_column(tmp_path: Path) -> None:
    path = tmp_path / "v1_hub.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE workflow (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                policy_json TEXT NOT NULL DEFAULT '{}',
                created TEXT NOT NULL
            );
            CREATE TABLE agent (
                name TEXT PRIMARY KEY,
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                context_id TEXT UNIQUE,
                last_seen TEXT NOT NULL,
                current_task_id TEXT
            );
            CREATE TABLE task (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                assignee TEXT,
                role TEXT NOT NULL,
                title TEXT NOT NULL,
                instructions TEXT NOT NULL,
                state TEXT NOT NULL,
                lease_expires TEXT,
                result_json TEXT,
                created TEXT NOT NULL,
                updated TEXT NOT NULL
            );
            CREATE TABLE message (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                context_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                direction TEXT NOT NULL,
                parts_json TEXT NOT NULL,
                ts TEXT NOT NULL
            );
            CREATE TABLE event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                ts TEXT NOT NULL
            );
            CREATE TABLE decision (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                summary TEXT NOT NULL,
                rationale TEXT NOT NULL
            );
            PRAGMA user_version = 1;
        """)
        connection.execute(
            "INSERT INTO agent (name, capabilities_json, status, context_id, last_seen) "
            "VALUES ('bob', '[]', 'idle', 'ctx-1', '2026-09-07T00:00:00Z')"
        )

    initialize_database(path)

    with database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(agent)").fetchall()]
        row = connection.execute("SELECT * FROM agent WHERE name = 'bob'").fetchone()

    assert version == SCHEMA_VERSION
    assert "runtime" in columns
    assert row["runtime"] is None

    from agent_hub.store import HubStore

    store = HubStore(path)
    agent = store.agent_by_name("bob")
    assert agent is not None
    assert agent.runtime is None
    assert agent.name == "bob"

    store.check_in("bob", ["python"], runtime="codex")
    updated = store.agent_by_name("bob")
    assert updated is not None
    assert updated.runtime == "codex"
