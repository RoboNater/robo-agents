"""FastAPI application: A2A discovery plus the protected worker protocol."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent_hub_common import HubSettings, load_or_create_token
from fastapi import Depends, FastAPI, Request, Response

from .card import build_agent_card
from .database import initialize_database
from .protocol import A2AProtocol, parse_error_response
from .security import require_bearer
from .signals import Signals
from .store import HubStore
from .sweeper import start_sweeper, stop_sweeper

logger = logging.getLogger(__name__)


def create_app(settings: HubSettings | None = None) -> FastAPI:
    """Create a configured hub application without starting a server."""

    resolved = settings or HubSettings.from_env()
    card = build_agent_card(resolved.public_url)
    store = HubStore(path=resolved.database_path, signals=Signals())
    protocol = A2AProtocol(store=store, settings=resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        initialize_database(resolved.database_path)
        app.state.bearer_token = load_or_create_token(resolved.token, resolved.token_file)
        # Log the resolved absolute path so a hub started against the wrong state
        # directory is visible at once, rather than as lost state later.
        logger.info("SQLite database ready at %s", resolved.database_path)
        if resolved.token is None:
            logger.info("Bearer token ready at %s", resolved.token_file)
        else:
            logger.info("Bearer token loaded from HUB_TOKEN")
        sweeper = start_sweeper(store, resolved.sweep_interval_s, resolved.heartbeat_timeout_s)
        try:
            yield
        finally:
            await stop_sweeper(sweeper)

    app = FastAPI(
        title="Agent Comms Hub",
        version="0.1.0",
        lifespan=lifespan,
        # Every route is hand-written and excluded from the schema, so the
        # generated docs would describe nothing while widening the public
        # surface past the two routes §4.1 allows.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
    app.state.store = store

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    async def agent_card() -> dict[str, object]:
        return card.model_dump(by_alias=True, exclude_none=True)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/a2a", include_in_schema=False, dependencies=[Depends(require_bearer)])
    async def a2a(request: Request) -> Response:
        try:
            payload: Any = await request.json()
        except ValueError:
            return parse_error_response()
        return await protocol.dispatch(payload)

    return app
