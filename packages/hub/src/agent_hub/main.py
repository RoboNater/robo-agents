"""Console entry point for the hub service."""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from typing import TextIO

import uvicorn
from agent_hub_common import HubSettings

from .app import create_app


@contextmanager
def reserve_stdout() -> Iterator[TextIO]:
    """Keep the original stream for MCP; send ordinary Python output to stderr.

    Step 3 must pass the yielded stream explicitly to its stdio transport,
    rather than letting the transport discover the redirected sys.stdout.
    This guards Python stream writes, not native writes to file descriptor 1
    or deliberate writes through sys.__stdout__.
    """
    protocol_stdout = sys.stdout
    with redirect_stdout(sys.stderr):
        yield protocol_stdout


def main() -> None:
    with reserve_stdout():
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        settings = HubSettings.from_env()
        log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
        for handler in log_config["handlers"].values():
            handler["stream"] = "ext://sys.stderr"
        # Step 3 will run uvicorn.Server alongside MCP in one asyncio loop,
        # passing reserve_stdout's original stream only to the MCP transport.
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            log_config=log_config,
            access_log=False,
        )


if __name__ == "__main__":
    main()
