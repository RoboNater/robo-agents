"""Console entry point for the hub service."""

import asyncio
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from typing import TextIO

import uvicorn
from agent_hub_common import HubSettings

from .app import create_app
from .mcp import run_mcp


@contextmanager
def reserve_stdout() -> Iterator[TextIO]:
    """Keep the original stream for MCP; send ordinary Python output to stderr.

    The MCP transport receives the yielded stream explicitly instead of
    discovering the redirected sys.stdout.
    This guards Python stream writes, not native writes to file descriptor 1
    or deliberate writes through sys.__stdout__.
    """
    protocol_stdout = sys.stdout
    with redirect_stdout(sys.stderr):
        yield protocol_stdout


async def run_hub(settings: HubSettings, stdout: TextIO) -> None:
    """Own both transports; either one's exit shuts down its sibling."""
    app = create_app(settings)
    log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    for handler in log_config["handlers"].values():
        handler["stream"] = "ext://sys.stderr"
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_config=log_config,
            access_log=False,
            timeout_graceful_shutdown=2,
        )
    )
    http = asyncio.create_task(server.serve())
    mcp: asyncio.Task[None] | None = None
    try:
        while not server.started:
            if http.done():
                await http
                raise RuntimeError("HTTP server stopped before startup")
            await asyncio.sleep(0.01)
        mcp = asyncio.create_task(run_mcp(app.state.store, stdout))
        done, _ = await asyncio.wait((http, mcp), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        if mcp is not None:
            mcp.cancel()
            await asyncio.gather(mcp, return_exceptions=True)
        server.should_exit = True
        await http


def main() -> None:
    with reserve_stdout() as stdout:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        asyncio.run(run_hub(HubSettings.from_env(), stdout))


if __name__ == "__main__":
    main()
