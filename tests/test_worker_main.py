from unittest.mock import AsyncMock, patch

import pytest
from agent_hub_common import ConfigurationError
from worker_mcp import WorkerSettings, main


def test_main_raises_on_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_URL", raising=False)
    monkeypatch.delenv("HUB_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)

    with pytest.raises(ConfigurationError, match="HUB_URL must be set"):
        WorkerSettings.from_env()


def test_main_runs_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_URL", "http://127.0.0.1:8420")
    monkeypatch.setenv("HUB_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_NAME", "bob")

    mock_run = AsyncMock(return_value=None)
    monkeypatch.setattr(main, "run_worker", mock_run)

    main.main()
    mock_run.assert_awaited_once()


async def test_run_worker_initializes_client_and_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_URL", "http://127.0.0.1:8420")
    monkeypatch.setenv("HUB_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_NAME", "bob")

    with (
        patch("worker_mcp.main.WorkerHubClient") as mock_client_cls,
        patch("worker_mcp.main.create_worker_mcp") as mock_create_mcp,
    ):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_server = AsyncMock()
        mock_create_mcp.return_value = mock_server

        await main.run_worker()

        mock_client_cls.assert_called_once()
        mock_create_mcp.assert_called_once_with(mock_client)
        mock_server.run_stdio_async.assert_awaited_once()
