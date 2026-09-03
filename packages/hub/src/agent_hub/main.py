"""Console entry point for the hub service."""

import uvicorn
from agent_hub_common import HubSettings

from .app import create_app


def main() -> None:
    settings = HubSettings.from_env()
    # Step 3 will run uvicorn.Server alongside MCP stdio in one asyncio loop.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
