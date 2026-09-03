"""Environment-backed configuration shared by hub processes."""

from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# A bind address that means "every interface" is never dialable, so the
# advertised URL has to be supplied separately. Unspecified IP literals are
# detected by value; "*" is a spelling no IP parser accepts.
WILDCARD_HOST_ALIAS = "*"


_HostAddress = ipaddress.IPv4Address | ipaddress.IPv6Address | None


class ConfigurationError(ValueError):
    """Raised when a hub environment variable is invalid."""


def _path(value: str, base: Path) -> Path:
    """Resolve a configured path, treating a relative value as base-relative."""

    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _host_address(host: str) -> _HostAddress:
    """Parse a bind host as an IP literal, tolerating IPv6 brackets."""

    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(literal)
    except ValueError:
        return None


def _authority(host: str, address: _HostAddress, port: int) -> str:
    """Render a normalized bind host as a URL authority."""

    if isinstance(address, ipaddress.IPv6Address):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _default_state_dir(env: Mapping[str, str]) -> Path:
    xdg_state_home = env.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg_state_home).expanduser() if xdg_state_home else Path()
    if not base.is_absolute():
        # The XDG base-directory specification declares a relative
        # XDG_STATE_HOME invalid and requires the default to be used instead.
        if xdg_state_home:
            logger.warning(
                "Ignoring relative XDG_STATE_HOME %r; using the default state directory",
                xdg_state_home,
            )
        base = Path.home() / ".local" / "state"
    return base / "agent-hub"


def _state_dir(env: Mapping[str, str]) -> Path:
    raw_state_dir = env.get("HUB_STATE_DIR", "").strip()
    if not raw_state_dir:
        return _default_state_dir(env)
    state_dir = Path(raw_state_dir).expanduser()
    if not state_dir.is_absolute():
        raise ConfigurationError(
            f"HUB_STATE_DIR must be an absolute path, got {raw_state_dir!r}; "
            "a relative state directory changes with the working directory"
        )
    return state_dir


@dataclass(frozen=True, slots=True)
class HubSettings:
    """Settings for the HTTP hub and its durable state."""

    host: str
    port: int
    public_url: str
    state_dir: Path
    database_path: Path
    token: str | None
    token_file: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> HubSettings:
        env = os.environ if environ is None else environ
        raw_host = env.get("HUB_HOST", "127.0.0.1").strip()
        if not raw_host:
            raise ConfigurationError("HUB_HOST cannot be empty")
        # An IP literal is stored in the only form the socket resolver accepts:
        # unbracketed and compressed. Brackets go back on for URL authorities.
        address = _host_address(raw_host)
        host = raw_host if address is None else address.compressed

        raw_port = env.get("HUB_PORT", "8420")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigurationError("HUB_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError("HUB_PORT must be between 1 and 65535")

        # The bind address and the advertised address are different concepts;
        # deriving one from the other is only correct for a specific interface.
        raw_public_url = env.get("HUB_PUBLIC_URL")
        if raw_public_url is None:
            if host == WILDCARD_HOST_ALIAS or (address is not None and address.is_unspecified):
                raise ConfigurationError(
                    f"HUB_PUBLIC_URL must be set when HUB_HOST is the wildcard address "
                    f"{raw_host}; workers cannot dial a bind address"
                )
            public_url = f"http://{_authority(host, address, port)}"
        else:
            public_url = raw_public_url.strip().rstrip("/")
            if not public_url.startswith(("http://", "https://")):
                raise ConfigurationError("HUB_PUBLIC_URL must be an http(s) URL")

        token = env.get("HUB_TOKEN")
        if token is not None:
            token = token.strip()
            if not token:
                raise ConfigurationError("HUB_TOKEN cannot be empty")

        # Durable state is anchored to an absolute directory and never to the
        # working directory, so an MCP client that picks its own cwd still
        # finds the database and token of the previous run.
        state_dir = _state_dir(env)

        return cls(
            host=host,
            port=port,
            public_url=public_url,
            state_dir=state_dir,
            database_path=_path(env.get("HUB_DB_PATH", "hub.db"), state_dir),
            token=token,
            token_file=_path(env.get("HUB_TOKEN_FILE", "token"), state_dir),
        )
