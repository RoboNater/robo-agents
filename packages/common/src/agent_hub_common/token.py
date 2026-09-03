"""Pre-shared bearer token provisioning and comparison."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
import secrets


class TokenError(RuntimeError):
    """Raised when a bearer token cannot be loaded safely."""


def _read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TokenError(f"cannot read bearer token file: {path}") from exc
    if not token:
        raise TokenError(f"bearer token file is empty: {path}")
    return token


def load_or_create_token(explicit_token: str | None, token_file: Path) -> str:
    """Return an injected token, or atomically create/read a mode-0600 token file."""

    if explicit_token is not None:
        if not explicit_token:
            raise TokenError("explicit bearer token cannot be empty")
        return explicit_token

    token_file.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(token_file, flags, 0o600)
    except FileExistsError:
        return _read_token(token_file)
    except OSError as exc:
        raise TokenError(f"cannot create bearer token file: {token_file}") from exc

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{generated}\n")
    except OSError as exc:
        raise TokenError(f"cannot write bearer token file: {token_file}") from exc
    return generated


def token_matches(candidate: str, expected: str) -> bool:
    """Compare bearer tokens without content-dependent early exit."""

    return hmac.compare_digest(candidate.encode(), expected.encode())
