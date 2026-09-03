"""Console entry point for the hub service."""

import uvicorn

from agent_hub_common import HubSettings


def main() -> None:
    settings = HubSettings.from_env()
    uvicorn.run(
        "agent_hub.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
