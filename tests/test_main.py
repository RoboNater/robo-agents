import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from agent_hub import main
from fastapi import FastAPI


def test_main_parses_environment_once_and_runs_built_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[FastAPI, dict[str, object]]] = []

    def fake_run(app: FastAPI, **kwargs: object) -> None:
        calls.append((app, kwargs))

    monkeypatch.setenv("HUB_HOST", "127.0.0.2")
    monkeypatch.setenv("HUB_PORT", "8430")
    monkeypatch.setenv("HUB_PUBLIC_URL", "https://public.example/")
    monkeypatch.setenv("HUB_DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("HUB_TOKEN", "test-token")
    monkeypatch.setattr("agent_hub.main.uvicorn.run", fake_run)

    main.main()

    app, kwargs = calls[0]
    assert isinstance(app, FastAPI)
    assert app.state.settings.public_url == "https://public.example"
    assert kwargs["host"] == "127.0.0.2"
    assert kwargs["port"] == 8430
    assert kwargs["access_log"] is False
    config = kwargs["log_config"]
    assert isinstance(config, dict)
    assert all(h["stream"] == "ext://sys.stderr" for h in config["handlers"].values())


def test_reserved_stream_is_exclusive_and_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    with pytest.raises(RuntimeError), main.reserve_stdout() as protocol:
        print("diagnostic")
        protocol.write('{"jsonrpc":"2.0"}\n')
        raise RuntimeError("shutdown")
    assert stdout.getvalue() == '{"jsonrpc":"2.0"}\n'
    assert stderr.getvalue() == "diagnostic\n"
    assert sys.stdout is stdout


def test_live_server_keeps_stdout_empty(tmp_path: Path) -> None:
    # Run in isolation so logging configuration and sys.stdout are process-local.
    # A pre-bound ephemeral socket avoids fixed-port conflicts and allocation races.
    script = r"""
import asyncio
import logging
import socket
import sys
import traceback

import httpx
import uvicorn
from agent_hub import main

async def serve(app, kwargs):
    kwargs["access_log"] = True  # Explicit stderr config must protect this too.
    server = uvicorn.Server(uvicorn.Config(app, **kwargs))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("server failed to start")
                await asyncio.sleep(0.01)
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"http://127.0.0.1:{listener.getsockname()[1]}/healthz"
                )
                assert response.json() == {"status": "ok"}
        finally:
            server.should_exit = True
            await task

def run(app, **kwargs):
    print("stray startup print")
    logger = logging.getLogger("third-party")
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.warning("third-party stdout handler")
    try:
        raise ValueError("callback traceback")
    except ValueError:
        traceback.print_exc(file=sys.stdout)
    asyncio.run(serve(app, kwargs))
    print("stray shutdown print")

main.uvicorn.run = run
main.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "HUB_STATE_DIR": str(tmp_path),
            "HUB_DB_PATH": str(tmp_path / "hub.db"),
            "HUB_TOKEN": "test-token",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    for marker in (
        "stray startup print",
        "third-party stdout handler",
        "callback traceback",
        "GET /healthz HTTP/1.1",
        "stray shutdown print",
        "Finished server process",
    ):
        assert marker in result.stderr
