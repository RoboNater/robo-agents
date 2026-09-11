import sqlite3
from pathlib import Path

import pytest
from agent_hub.database import (
    SCHEMA_VERSION,
    DatabaseVersionError,
    database,
    initialize_database,
)
from agent_hub.store import HubStore
from agent_hub_common import UNKNOWN, AgentProfile, ModelSource


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


def _legacy_database(path: Path, version: int) -> None:
    """Write a Step 1 (v1) or Step 4 (v2, with `agent.runtime`) schema."""

    runtime_column = ",\n                runtime TEXT" if version == 2 else ""
    with sqlite3.connect(path) as connection:
        connection.executescript(f"""
            CREATE TABLE workflow (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                policy_json TEXT NOT NULL DEFAULT '{{}}',
                created TEXT NOT NULL
            );
            CREATE TABLE agent (
                name TEXT PRIMARY KEY,
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                context_id TEXT UNIQUE,
                last_seen TEXT NOT NULL,
                current_task_id TEXT{runtime_column}
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
            PRAGMA user_version = {version};
        """)


def _agent_columns(path: Path) -> dict[str, tuple[str, int, str | None]]:
    with database(path) as connection:
        rows = connection.execute("PRAGMA table_info(agent)").fetchall()
    return {row["name"]: (row["type"], row["notnull"], row["dflt_value"]) for row in rows}


def test_migration_from_v1_adds_an_unknown_profile(tmp_path: Path) -> None:
    path = tmp_path / "v1_hub.db"
    _legacy_database(path, version=1)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO agent (name, capabilities_json, status, context_id, last_seen) "
            "VALUES ('bob', '[]', 'idle', 'ctx-1', '2026-09-07T00:00:00Z')"
        )

    initialize_database(path)

    with database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION

    store = HubStore(path)
    agent = store.agent_by_name("bob")
    assert agent is not None
    assert agent.name == "bob"
    assert (agent.harness, agent.provider, agent.model) == (UNKNOWN, UNKNOWN, UNKNOWN)
    assert agent.model_source is ModelSource.UNKNOWN
    assert agent.workspace_id is None

    store.check_in("bob", AgentProfile(harness="codex"))
    updated = store.agent_by_name("bob")
    assert updated is not None
    assert updated.harness == "codex"


def test_migration_from_v2_carries_the_runtime_over_as_the_harness(tmp_path: Path) -> None:
    path = tmp_path / "v2_hub.db"
    _legacy_database(path, version=2)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO agent (name, capabilities_json, status, context_id, last_seen, runtime) "
            "VALUES (?, '[]', 'idle', ?, '2026-09-07T00:00:00Z', ?)",
            [
                ("bob", "ctx-1", "claude-code"),
                ("charlie", "ctx-2", "codex"),
                ("dan", "ctx-3", None),
            ],
        )

    initialize_database(path)
    initialize_database(path)

    assert "runtime" not in _agent_columns(path)
    store = HubStore(path)
    harnesses = {agent.name: agent.harness for agent in store.agents()}
    assert harnesses == {"bob": "claude-code", "charlie": "codex", "dan": UNKNOWN}


@pytest.mark.parametrize("version", [1, 2])
def test_migrated_agent_table_matches_a_fresh_one(tmp_path: Path, version: int) -> None:
    fresh = tmp_path / "fresh.db"
    migrated = tmp_path / f"v{version}.db"
    initialize_database(fresh)
    _legacy_database(migrated, version)

    initialize_database(migrated)

    assert _agent_columns(migrated) == _agent_columns(fresh)


def test_model_source_is_constrained_to_its_enum(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)

    with pytest.raises(sqlite3.IntegrityError), database(path) as connection:
        connection.execute(
            "INSERT INTO agent (name, status, context_id, last_seen, model_source) "
            "VALUES ('bob', 'idle', 'ctx-1', '2026-09-07T00:00:00Z', 'guessed')"
        )
