import sqlite3
from pathlib import Path

import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import (
    PROFILE_COLUMNS,
    SCHEMA_VERSION,
    DatabaseVersionError,
    database,
    initialize_database,
)
from agent_hub.store import HubStore
from agent_hub_common import UNKNOWN, AgentProfile, HubSettings, MetaKeys, ModelSource
from conftest import BASE_URL, TOKEN


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

    assert {"workflow", "agent", "task", "message", "event", "decision", "operation"} <= tables
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
    """Write a Step 1 (v1), Step 4 (v2, with runtime), or main (v3, with profile) schema."""

    if version == 1:
        extra_columns = ""
    elif version == 2:
        extra_columns = ",\n                runtime TEXT"
    elif version in (3, 4):
        extra_columns = "".join(
            f",\n                {name} {spec}" for name, spec in PROFILE_COLUMNS.items()
        )
    else:
        raise ValueError(f"unsupported legacy version {version}")

    with sqlite3.connect(path) as connection:
        operation_table = """
            CREATE TABLE operation (
                actor TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created TEXT NOT NULL,
                PRIMARY KEY (actor, operation_id)
            );
        """ if version == 4 else ""
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
                current_task_id TEXT{extra_columns}
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
            {operation_table}
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

def test_migration_from_v2_adds_operation_table(tmp_path: Path) -> None:
    path = tmp_path / "v2_hub.db"
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
                current_task_id TEXT,
                runtime TEXT
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
            PRAGMA user_version = 2;
        """)

    initialize_database(path)

    with database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        # Insert into operation table to verify schema
        connection.execute(
            "INSERT INTO operation (actor, operation_id, payload_hash, response_json, created) "
            "VALUES ('bob', 'op-1', 'hash-1', '{\"ok\": true}', '2026-09-07T00:00:00Z')"
        )
        row = connection.execute("SELECT * FROM operation WHERE actor = 'bob'").fetchone()

    assert version == SCHEMA_VERSION
    assert "operation" in tables
    assert row["operation_id"] == "op-1"
    assert row["payload_hash"] == "hash-1"
    assert row["created"] == "2026-09-07T00:00:00Z"


@pytest.mark.parametrize("version", [1, 2, 3, 4])
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


async def test_migration_from_v3_mainline_preserves_profiles_and_enables_idempotent_check_in(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v3_mainline.db"
    _legacy_database(path, version=3)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO agent ("
            "    name, capabilities_json, status, context_id, last_seen, "
            "    harness, harness_version, provider, model, model_source, workspace_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "bob",
                '["python", "fastapi"]',
                "idle",
                "ctx-bob-1",
                "2026-09-10T20:00:00Z",
                "claude-code",
                "1.0.0",
                "anthropic",
                "claude-3-7-sonnet",
                "env",
                "ws-bob-main",
            ),
        )

    # Migrate from v3 through the composed migrations to v5.
    initialize_database(path)

    # 1. Verify version advanced to the current schema.
    with database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        op_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(operation)").fetchall()
        }

    assert version == SCHEMA_VERSION  # 5
    assert "operation" in tables
    assert {"actor", "operation_id", "payload_hash", "response_json", "created"} <= op_columns

    # 2. Verify profile data survived intact
    store = HubStore(path)
    bob = store.agent_by_name("bob")
    assert bob is not None
    assert bob.name == "bob"
    assert bob.capabilities == ["python", "fastapi"]
    assert bob.status.value == "idle"
    assert bob.harness == "claude-code"
    assert bob.harness_version == "1.0.0"
    assert bob.provider == "anthropic"
    assert bob.model == "claude-3-7-sonnet"
    assert bob.model_source == ModelSource.ENV
    assert bob.workspace_id == "ws-bob-main"
    assert bob.last_heartbeat == "2026-09-10T20:00:00Z"
    assert bob.worker_instance_id == ""

    # 3. Verify an idempotent wire check-in succeeds on the migrated database
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=path,
        token=TOKEN,
        token_file=tmp_path / "token",
        guides_dir=guides_dir,
        default_wait_s=0.2,
        max_wait_s=1.0,
        lost_after_s=60.0,
        sweep_interval_s=3600.0,
    )
    app = create_app(settings)
    op_id = "op-v3-migration-wire-checkin"
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "msg-v3-1",
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.AGENT: "bob",
                    MetaKeys.CAPABILITIES: ["python", "fastapi"],
                    MetaKeys.HARNESS: "claude-code",
                    MetaKeys.HARNESS_VERSION: "1.0.0",
                    MetaKeys.PROVIDER: "anthropic",
                    MetaKeys.MODEL: "claude-3-7-sonnet",
                    MetaKeys.MODEL_SOURCE: "env",
                    MetaKeys.WORKSPACE_ID: "ws-bob-main",
                    MetaKeys.SCHEMA_VERSION: 1,
                    MetaKeys.OPERATION_ID: op_id,
                    MetaKeys.WORKER_INSTANCE_ID: "worker-v3-migration",
                },
            }
        },
    }

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        resp1 = await client.post("/a2a", json=payload)
        assert resp1.status_code == 200
        res1 = resp1.json()["result"]
        assert res1["kind"] == "message"
        assert res1["metadata"][MetaKeys.AGENT] == "bob"

        # Verify operation record created
        with database(path) as conn:
            op_row = conn.execute(
                "SELECT * FROM operation WHERE actor = 'bob' AND operation_id = ?", (op_id,)
            ).fetchone()
            assert op_row is not None
            assert op_row["operation_id"] == op_id
            assert op_row["created"] != ""

        # Replay identical wire check-in returns cached result
        resp2 = await client.post("/a2a", json=payload)
        assert resp2.status_code == 200
        assert resp2.json()["result"] == res1
