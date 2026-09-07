import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import initialize_database
from agent_hub.store import HubStore
from agent_hub_common import HubSettings, WorkflowStatus
from conftest import BASE_URL, TOKEN
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings

script_path = Path(__file__).resolve().parents[1] / "scripts" / "mock-alice.py"
spec = importlib.util.spec_from_file_location("mock_alice", script_path)
assert spec is not None and spec.loader is not None
mock_alice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_alice)


@pytest.mark.parametrize(
    ("agent_name", "runtime"),
    [
        ("bob", "claude-code"),
        ("charlie", "codex"),
    ],
)
async def test_mock_alice_drives_worker_through_full_task(
    agent_name: str,
    runtime: str,
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
        heartbeat_timeout_s=60.0,
        sweep_interval_s=3600.0,
    )
    app = create_app(settings)
    store = app.state.store

    worker_settings = WorkerSettings(
        hub_url=BASE_URL,
        token=TOKEN,
        agent_name=agent_name,
        runtime=runtime,
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
                expected_runtime=runtime,
            )
        )
        worker_task = asyncio.create_task(run_worker())

        alice_res, _ = await asyncio.gather(alice_task, worker_task)

        assert alice_res["task_id"] is not None
        assert store.get_state()["workflow"]["status"] == WorkflowStatus.DONE.value


async def test_mock_alice_rejects_unexpected_runtime(tmp_path: Path) -> None:
    db_path = tmp_path / "hub_mismatch.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    # Bob checks in as claude-code
    store.check_in("bob", ["python"], runtime="claude-code")

    # Mock Alice expects codex
    match_msg = "Worker 'bob' checked in with runtime 'claude-code', expected 'codex'"
    with pytest.raises(ValueError, match=match_msg):
        await mock_alice.drive_one_task(
            store=store,
            expected_agent="bob",
            timeout_s=1.0,
            expected_runtime="codex",
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
        assert kwargs.get("expected_runtime") == "codex"
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
