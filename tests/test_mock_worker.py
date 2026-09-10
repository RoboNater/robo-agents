import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import initialize_database
from agent_hub_common import HubSettings, TaskState
from conftest import BASE_URL, TOKEN
from worker_mcp.config import WorkerSettings

script_path = Path(__file__).resolve().parents[1] / "scripts" / "mock-worker.py"
spec = importlib.util.spec_from_file_location("mock_worker", script_path)
assert spec is not None and spec.loader is not None
mock_worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_worker)


async def test_mock_worker_implements_task_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "hub_worker.db"
    initialize_database(db_path)
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "implementer.md").write_text("# Implementer Guide", encoding="utf-8")

    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=db_path,
        token=TOKEN,
        token_file=tmp_path / "token",
        guides_dir=guides_dir,
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
        agent_name="bob",
        runtime="claude-code",
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
        async def orchestrator() -> None:
            # Wait for bob to check in
            for _ in range(100):
                ag = store.agent_by_name("bob")
                if ag is not None:
                    break
                await asyncio.sleep(0.01)

            # Assign task
            task = store.assign_task("bob", "implementer", "Implement feature", "Write code")

            # Wait for task completion
            for _ in range(100):
                t = store.get_task(task.id)
                if t is not None and t.state == TaskState.COMPLETED:
                    break
                await asyncio.sleep(0.01)

            # Release bob
            store.release_agent("bob")

        worker_coro = mock_worker.run_worker(
            worker_settings,
            http_client=http_client,
            timeout_s=5.0,
        )

        orch_task = asyncio.create_task(orchestrator())
        worker_task = asyncio.create_task(worker_coro)

        _, worker_res = await asyncio.gather(orch_task, worker_task)

        assert worker_res.get("status") == "completed"
        assert worker_res.get("result", {}).get("outcome") == "completed"
        assert worker_res.get("result", {}).get("pr_url") is not None


async def test_mock_worker_reviewer_task(tmp_path: Path) -> None:
    db_path = tmp_path / "hub_reviewer.db"
    initialize_database(db_path)
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "reviewer.md").write_text("# Reviewer Guide", encoding="utf-8")

    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=db_path,
        token=TOKEN,
        token_file=tmp_path / "token",
        guides_dir=guides_dir,
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
        agent_name="charlie",
        runtime="codex",
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
        async def orchestrator() -> None:
            for _ in range(100):
                ag = store.agent_by_name("charlie")
                if ag is not None:
                    break
                await asyncio.sleep(0.01)

            task = store.assign_task("charlie", "reviewer", "Review PR", "Review diff")

            for _ in range(100):
                t = store.get_task(task.id)
                if t is not None and t.state == TaskState.COMPLETED:
                    break
                await asyncio.sleep(0.01)

            store.release_agent("charlie")

        worker_coro = mock_worker.run_worker(
            worker_settings,
            http_client=http_client,
            timeout_s=5.0,
        )

        orch_task = asyncio.create_task(orchestrator())
        worker_task = asyncio.create_task(worker_coro)

        _, worker_res = await asyncio.gather(orch_task, worker_task)

        assert worker_res.get("status") == "completed"
        assert worker_res.get("result", {}).get("verdict") == "approved"
        assert worker_res.get("result", {}).get("reviewed_head_sha") is not None


def test_mock_worker_main_cli_parses_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    called = False

    def fake_run(coro: object) -> dict[str, str]:
        nonlocal called
        called = True
        if hasattr(coro, "close"):
            coro.close()
        return {"status": "ok"}

    monkeypatch.setattr(mock_worker.asyncio, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mock-worker.py",
            "--hub-url",
            "http://127.0.0.1:8420",
            "--agent",
            "charlie",
            "--runtime",
            "codex",
            "--timeout",
            "10",
        ],
    )
    mock_worker.main()
    assert called is True
