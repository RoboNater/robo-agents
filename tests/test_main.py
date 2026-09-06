import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
import uvicorn
from agent_hub import main


def test_main_parses_environment_once_and_reserves_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typing import TextIO

    from agent_hub_common import HubSettings

    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    async def fake_run(settings: HubSettings, protocol: TextIO) -> None:
        assert settings.public_url == "https://public.example"
        assert settings.host == "127.0.0.2"
        assert settings.port == 8430
        assert protocol is stdout
        assert sys.stdout is stderr

    monkeypatch.setenv("HUB_HOST", "127.0.0.2")
    monkeypatch.setenv("HUB_PORT", "8430")
    monkeypatch.setenv("HUB_PUBLIC_URL", "https://public.example/")
    monkeypatch.setenv("HUB_DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("HUB_TOKEN", "test-token")
    monkeypatch.setattr(main, "run_hub", fake_run)
    main.main()


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


def test_signal_capture_degrades_off_main_thread() -> None:
    import threading

    errors: list[BaseException] = []
    server = main.HubServer(uvicorn.Config("unused:app"))

    def enter() -> None:
        try:
            with server.capture_signals():
                pass
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=enter)
    thread.start()
    thread.join()
    assert errors == []


def test_second_sigint_forces_exit() -> None:
    import signal

    server = main.HubServer(uvicorn.Config("unused:app"))
    with server.capture_signals():
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert server.should_exit and not server.force_exit
        handler(signal.SIGINT, None)
        assert server.force_exit


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

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()
import os
os.environ["HUB_PORT"] = str(port)

original_config = uvicorn.Config
def config(*args, **kwargs):
    kwargs["access_log"] = True
    return original_config(*args, **kwargs)
main.uvicorn.Config = config

async def fake_mcp(store, stdout):
    print("stray startup print")
    logger = logging.getLogger("third-party")
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.warning("third-party stdout handler")
    try:
        raise ValueError("callback traceback")
    except ValueError:
        traceback.print_exc(file=sys.stdout)
    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{port}/healthz")
        assert response.json() == {"status": "ok"}
    print("stray shutdown print")
    return True

main.run_mcp = fake_mcp
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


@pytest.mark.parametrize("shutdown", ["eof", "sigterm", "sigint", "http_error", "mcp_stuck"])
def test_process_shutdown_releases_listener(tmp_path: Path, shutdown: str) -> None:
    import signal
    import socket
    import time
    import urllib.request

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    script = """
import asyncio
from agent_hub import main
from agent_hub import app

original_stop = app.stop_sweeper
async def stop(task):
    await original_stop(task)
    print("sweeper stopped")
app.stop_sweeper = stop
"""
    if shutdown == "http_error":
        script += """
async def broken_loop(self):
    await asyncio.sleep(0.5)
    raise RuntimeError("injected HTTP failure")
main.uvicorn.Server.main_loop = broken_loop
"""
    if shutdown == "mcp_stuck":
        script += """
async def stuck_mcp(store, stdout):
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
main.run_mcp = stuck_mcp
"""
    script += "\nmain.main()"
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "HUB_STATE_DIR": str(tmp_path),
            "HUB_DB_PATH": str(tmp_path / "hub.db"),
            "HUB_TOKEN": "test",
            "HUB_HOST": "127.0.0.1",
            "HUB_PORT": str(port),
        },
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.2):
                    break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.02)
        if shutdown == "eof":
            assert process.stdin is not None
            process.stdin.close()
        elif shutdown not in ("http_error", "mcp_stuck"):
            process.send_signal(signal.SIGINT if shutdown == "sigint" else signal.SIGTERM)
        elif shutdown == "mcp_stuck":
            process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
        assert process.stderr is not None
        stderr = process.stderr.read().decode()
        assert "sweeper stopped" in stderr
        expected = 1 if shutdown in ("eof", "http_error", "mcp_stuck") else 0
        assert process.returncode == expected, stderr
        if shutdown == "eof":
            assert "MCP stdin closed before initialization" in stderr
        if shutdown == "http_error":
            assert "injected HTTP failure" in stderr
        if shutdown == "mcp_stuck":
            assert "MCP transport did not stop; forcing process exit" in stderr
        with pytest.raises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.parametrize("blocked_stdout", [False, True])
def test_initialized_mcp_disconnect_and_blocked_output_shutdown(
    tmp_path: Path, blocked_stdout: bool
) -> None:
    import json
    import signal
    import socket
    import time
    import urllib.request

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_hub.main"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={
            **os.environ,
            "HUB_STATE_DIR": str(tmp_path),
            "HUB_DB_PATH": str(tmp_path / "hub.db"),
            "HUB_TOKEN": "test",
            "HUB_HOST": "127.0.0.1",
            "HUB_PORT": str(port),
        },
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.2).close()
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.02)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["id"] == 1
        process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        if blocked_stdout:
            for request_id in range(2, 102):
                process.stdin.write(
                    json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}) + "\n"
                )
            process.stdin.flush()
            time.sleep(0.2)
            process.send_signal(signal.SIGTERM)
        else:
            process.stdin.close()
        process.wait(timeout=5)
        assert process.returncode == 0
        assert process.stderr is not None
        stderr = process.stderr.read()
        assert "Finished server process" in stderr
        if not blocked_stdout:
            assert "MCP client disconnected; shutting down HTTP" in stderr
            assert "before initialization" not in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
