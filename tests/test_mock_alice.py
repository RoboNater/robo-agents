import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import database, initialize_database
from agent_hub.store import HubStore
from agent_hub_common import AgentProfile, HubSettings, TaskState, WorkflowStatus
from conftest import BASE_URL, TOKEN
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings

script_path = Path(__file__).resolve().parents[1] / "scripts" / "mock-alice.py"
spec = importlib.util.spec_from_file_location("mock_alice", script_path)
assert spec is not None and spec.loader is not None
mock_alice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_alice)


@pytest.mark.parametrize(
    ("agent_name", "harness"),
    [
        ("bob", "claude-code"),
        ("charlie", "codex"),
    ],
)
async def test_mock_alice_drives_worker_through_full_task(
    agent_name: str,
    harness: str,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / f"hub_{agent_name}.db"
    initialize_database(db_path)
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
    )
    app = create_app(settings)
    store = app.state.store

    worker_settings = WorkerSettings(
        hub_url=BASE_URL,
        token=TOKEN,
        agent_name=agent_name,
        profile=AgentProfile(harness=harness),
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

        async def run_worker() -> None:
            # 1. Check in
            await worker.check_in(["python"])

            # 2. Wait for assignment
            assignment = await worker.await_assignment(timeout_s=2.0)
            assert assignment.get("task_id")
            task_id = assignment["task_id"]
            assert assignment["role"] == "implementer"

            # 3. Report progress
            prog = await worker.report_progress(task_id, "Working on it...")
            assert prog["ok"] is True

            # 4. Ask a question and receive answer
            q_res = await worker.ask_alice(task_id, "Confirm design?", timeout_s=2.0)
            assert "Approved" in q_res.get("reply", "")

            # 5. Submit result
            res = await worker.submit_result(
                task_id,
                "completed",
                "Feature implemented and tested",
                artifacts=[{"name": "pr", "url": "https://github.com/repo/pull/1"}],
            )
            assert res["status"] == "completed"

            # 6. Await assignment again -> should receive release
            rel = await worker.await_assignment(timeout_s=2.0)
            assert rel == {"release": True}

        alice_task = asyncio.create_task(
            mock_alice.drive_one_task(
                store=store,
                expected_agent=agent_name,
                role="implementer",
                title="Test Issue",
                instructions="Please fix the issue.",
                timeout_s=5.0,
                expected_harness=harness,
            )
        )
        worker_task = asyncio.create_task(run_worker())

        alice_res, _ = await asyncio.gather(alice_task, worker_task)

        assert alice_res["task_id"] is not None
        assert store.get_state()["workflow"]["status"] == WorkflowStatus.DONE.value
        with database(store.path) as connection:
            decisions = connection.execute("SELECT * FROM decision ORDER BY id").fetchall()
            assert any("Assigned task" in d["summary"] for d in decisions)


async def test_mock_alice_rejects_unexpected_harness(tmp_path: Path) -> None:
    db_path = tmp_path / "hub_mismatch.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    # Bob checks in as claude-code
    store.check_in("bob", AgentProfile(harness="claude-code"))

    # Mock Alice expects codex
    match_msg = "Worker 'bob' checked in with harness 'claude-code', expected 'codex'"
    with pytest.raises(ValueError, match=match_msg):
        await mock_alice.drive_one_task(
            store=store,
            expected_agent="bob",
            timeout_s=1.0,
            expected_harness="codex",
        )


def test_mock_alice_main_cli_parses_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "cli_test.db"
    initialize_database(db_path)

    called = False

    async def fake_drive_one_task(
        store: object, expected_agent: str, **kwargs: object
    ) -> dict[str, str]:
        nonlocal called
        called = True
        assert expected_agent == "charlie"
        assert kwargs.get("expected_harness") == "codex"
        return {"status": "ok"}

    monkeypatch.setattr(mock_alice, "drive_one_task", fake_drive_one_task)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mock-alice.py",
            "--db",
            str(db_path),
            "--agent",
            "charlie",
            "--runtime",
            "codex",
            "--timeout",
            "10",
        ],
    )

    mock_alice.main()
    assert called is True


async def test_mock_alice_agent_already_checked_in_event_consumed(tmp_path: Path) -> None:
    db_path = tmp_path / "already_checked_in.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    # Bob checks in as codex
    store.check_in("bob", AgentProfile(harness="codex"))

    # Consume the check_in event so wait_for_event returns None
    event = await store.wait_for_event(timeout_s=0.01)
    assert event is not None and event.kind.value == "agent_checked_in"

    # Worker completes task once assigned
    async def finish_task() -> None:
        for _ in range(100):
            t = store.get_state()["tasks"]
            if t:
                task_id = t[0]["id"]
                store.submit_result(
                    task_id=task_id,
                    agent="bob",
                    status=TaskState.COMPLETED,
                    summary="Done",
                )
                break
            await asyncio.sleep(0.01)

    drive_fut = asyncio.create_task(
        mock_alice.drive_one_task(
            store=store,
            expected_agent="bob",
            timeout_s=2.0,
            expected_harness="codex",
        )
    )
    finish_fut = asyncio.create_task(finish_task())
    res, _ = await asyncio.gather(drive_fut, finish_fut)
    assert res.get("summary") == "Done"


async def test_mock_alice_agent_already_checked_in_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "already_mismatch.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    # Bob checks in as codex
    store.check_in("bob", AgentProfile(harness="codex"))

    # Consume the check_in event
    await store.wait_for_event(timeout_s=0.01)

    match_msg = "Worker 'bob' checked in with harness 'codex', expected 'claude-code'"
    with pytest.raises(ValueError, match=match_msg):
        await mock_alice.drive_one_task(
            store=store,
            expected_agent="bob",
            timeout_s=1.0,
            expected_harness="claude-code",
        )


async def test_mock_alice_call_helper_error_handling() -> None:
    from unittest.mock import AsyncMock, MagicMock

    session = MagicMock()

    # Success call
    mock_success = MagicMock()
    mock_success.isError = False
    mock_success.structuredContent = {"ok": True}
    mock_success.content = None
    session.call_tool = AsyncMock(return_value=mock_success)

    data = await mock_alice._call(session, "get_state")
    assert data == {"ok": True}

    # Error call
    mock_error = MagicMock()
    mock_error.isError = True
    text_content = MagicMock()
    text_content.text = "Field required: summary"
    mock_error.content = [text_content]
    session.call_tool = AsyncMock(return_value=mock_error)

    with pytest.raises(RuntimeError, match="Tool log_decision failed: Field required: summary"):
        await mock_alice._call(session, "log_decision", {"decision": "wrong"})


def test_mock_alice_parse_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    # On Windows: parses paths with spaces inside quotes without stripping backslashes
    monkeypatch.setattr(sys, "platform", "win32")
    cmd_win = r'"C:\Program Files\Python312\python.exe" -m agent_hub.main'
    parts = mock_alice._parse_cmd(cmd_win)
    assert parts == [r"C:\Program Files\Python312\python.exe", "-m", "agent_hub.main"]

    # On POSIX: standard shlex.split
    monkeypatch.setattr(sys, "platform", "linux")
    cmd_posix = "/usr/bin/python3 -m agent_hub.main"
    assert mock_alice._parse_cmd(cmd_posix) == ["/usr/bin/python3", "-m", "agent_hub.main"]


def test_mock_alice_main_cli_mcp_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    called_mcp = False

    def fake_run(coro: object) -> dict[str, str]:
        nonlocal called_mcp
        called_mcp = True
        if hasattr(coro, "close"):
            coro.close()
        return {"status": "ok"}

    monkeypatch.setattr(mock_alice.asyncio, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mock-alice.py",
            "--mcp",
            "--agent",
            "bob",
            "--harness",
            "claude-code",
        ],
    )
    mock_alice.main()
    assert called_mcp is True
