from pathlib import Path

from fastapi.testclient import TestClient

from agent_hub import create_app
from agent_hub_common import HubSettings


def test_app_initializes_state_and_serves_agent_card(tmp_path: Path) -> None:
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        database_path=tmp_path / "hub.db",
        token=None,
        token_file=tmp_path / "token",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/.well-known/agent-card.json")
        health = client.get("/healthz")

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "Agent Comms Hub"
    assert card["url"] == "http://hub.example:8420/a2a"
    assert card["capabilities"]["streaming"] is True
    assert card["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert health.json() == {"status": "ok"}
    assert settings.database_path.exists()
    assert settings.token_file.exists()
