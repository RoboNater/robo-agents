"""FastAPI application for A2A discovery and future protocol routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from agent_hub_common import HubSettings, load_or_create_token

from .card import build_agent_card
from .database import initialize_database


def create_app(settings: HubSettings | None = None) -> FastAPI:
    """Create a configured hub application without starting a server."""

    resolved = settings or HubSettings.from_env()
    card = build_agent_card(resolved.public_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        initialize_database(resolved.database_path)
        app.state.bearer_token = load_or_create_token(resolved.token, resolved.token_file)
        yield

    app = FastAPI(
        title="Agent Comms Hub",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    async def agent_card() -> dict[str, object]:
        return card.model_dump(by_alias=True, exclude_none=True)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
