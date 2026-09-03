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
    assert kwargs == {"host": "127.0.0.2", "port": 8430, "access_log": False}
