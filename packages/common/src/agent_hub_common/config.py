"""Environment-backed configuration shared by hub processes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Binding one of these advertises an address no worker can dial, so the
# advertised URL has to be supplied separately.
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*"})


class ConfigurationError(ValueError):
    """Raised when a hub environment variable is invalid."""


def _path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _default_state_dir(env: Mapping[str, str], cwd: Path) -> Path:
    xdg_state_home = env.get("XDG_STATE_HOME", "").strip()
    base = _path(xdg_state_home, cwd) if xdg_state_home else Path.home() / ".local" / "state"
    return base / "agent-hub"


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
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        cwd: Path | None = None,
    ) -> HubSettings:
        env = os.environ if environ is None else environ
        base = Path.cwd() if cwd is None else cwd
        host = env.get("HUB_HOST", "127.0.0.1").strip()
        if not host:
            raise ConfigurationError("HUB_HOST cannot be empty")

        raw_port = env.get("HUB_PORT", "8420")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigurationError("HUB_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError("HUB_PORT must be between 1 and 65535")

        # The bind address and the advertised address are different concepts;
        # deriving one from the other is only correct for a loopback bind.
        raw_public_url = env.get("HUB_PUBLIC_URL")
        if raw_public_url is None:
            if host in WILDCARD_HOSTS:
                raise ConfigurationError(
                    f"HUB_PUBLIC_URL must be set when HUB_HOST is the wildcard address {host}; "
                    "workers cannot dial a bind address"
                )
            authority = f"[{host}]" if ":" in host else host
            public_url = f"http://{authority}:{port}"
        else:
            public_url = raw_public_url.strip().rstrip("/")
            if not public_url.startswith(("http://", "https://")):
                raise ConfigurationError("HUB_PUBLIC_URL must be an http(s) URL")

        token = env.get("HUB_TOKEN")
        if token is not None:
            token = token.strip()
            if not token:
                raise ConfigurationError("HUB_TOKEN cannot be empty")

        # Durable state is anchored to a fixed directory rather than the working
        # directory, so an MCP client that picks its own cwd still finds the
        # database and token of the previous run.
        raw_state_dir = env.get("HUB_STATE_DIR", "").strip()
        state_dir = _path(raw_state_dir, base) if raw_state_dir else _default_state_dir(env, base)

        return cls(
            host=host,
            port=port,
            public_url=public_url,
            state_dir=state_dir,
            database_path=_path(env.get("HUB_DB_PATH", "hub.db"), state_dir),
            token=token,
            token_file=_path(env.get("HUB_TOKEN_FILE", "token"), state_dir),
        )
