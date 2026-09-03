"""FastAPI application for A2A discovery and future protocol routes."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agent_hub_common import HubSettings, load_or_create_token
from fastapi import FastAPI

from .card import build_agent_card
from .database import initialize_database

logger = logging.getLogger(__name__)


def create_app(settings: HubSettings | None = None) -> FastAPI:
    """Create a configured hub application without starting a server."""

    resolved = settings or HubSettings.from_env()
    card = build_agent_card(resolved.public_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        initialize_database(resolved.database_path)
        app.state.bearer_token = load_or_create_token(resolved.token, resolved.token_file)
        if resolved.token is None:
            logger.info("Bearer token ready at %s", resolved.token_file)
        else:
            logger.info("Bearer token loaded from HUB_TOKEN")
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
