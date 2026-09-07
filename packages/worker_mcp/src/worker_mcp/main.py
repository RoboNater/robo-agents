"""Console entry point for worker-mcp."""

from __future__ import annotations

import logging
import sys
from contextlib import suppress

import anyio

from .client import WorkerHubClient
from .config import WorkerSettings
from .tools import create_worker_mcp


async def run_worker() -> None:
    settings = WorkerSettings.from_env()
    async with WorkerHubClient(settings) as client:
        server = create_worker_mcp(client)
        await server.run_stdio_async()


def main() -> None:
    # Ensure stdout is reserved exclusively for MCP framing; all logging goes to stderr.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    with suppress(KeyboardInterrupt):
        anyio.run(run_worker)


if __name__ == "__main__":
    main()
