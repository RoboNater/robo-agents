"""Environment-backed configuration shared by hub processes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when a hub environment variable is invalid."""


def _path(value: str, cwd: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else cwd / path


@dataclass(frozen=True, slots=True)
class HubSettings:
    """Settings for the HTTP hub and its durable state."""

    host: str
    port: int
    public_url: str
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

        public_url = env.get("HUB_PUBLIC_URL", f"http://{host}:{port}").rstrip("/")
        if not public_url.startswith(("http://", "https://")):
            raise ConfigurationError("HUB_PUBLIC_URL must be an http(s) URL")

        token = env.get("HUB_TOKEN")
        if token is not None:
            token = token.strip()
            if not token:
                raise ConfigurationError("HUB_TOKEN cannot be empty")

        return cls(
            host=host,
            port=port,
            public_url=public_url,
            database_path=_path(env.get("HUB_DB_PATH", ".hub/hub.db"), base),
            token=token,
            token_file=_path(env.get("HUB_TOKEN_FILE", ".hub/token"), base),
        )
