"""Console entry point for the hub service."""

import logging

import uvicorn
from agent_hub_common import HubSettings

from .app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = HubSettings.from_env()
    # Step 3 will run uvicorn.Server alongside MCP stdio in one asyncio loop;
    # HTTP access logs must remain disabled or be redirected away from stdout.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
