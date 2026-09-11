"""Environment-backed configuration for worker MCP processes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from agent_hub_common import AgentProfile, ConfigurationError, profile_from_env

DEFAULT_WAIT_S = 120.0
DEFAULT_HEARTBEAT_S = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR_S = 0.5


def _positive_seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number of seconds") from exc
    if seconds <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return seconds


def _non_negative_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise ConfigurationError(f"{name} must be zero or greater")
    return value


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Settings for the worker MCP client."""

    hub_url: str
    token: str
    agent_name: str
    # What the launcher says this worker is; reported at check-in (§4.3).
    profile: AgentProfile = field(default_factory=AgentProfile)
    default_wait_s: float = DEFAULT_WAIT_S
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_factor_s: float = DEFAULT_BACKOFF_FACTOR_S

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WorkerSettings:
        env = os.environ if environ is None else environ

        raw_hub_url = env.get("HUB_URL")
        if not raw_hub_url or not raw_hub_url.strip():
            raise ConfigurationError("HUB_URL must be set")
        hub_url = raw_hub_url.strip().rstrip("/")
        if not hub_url.startswith(("http://", "https://")):
            raise ConfigurationError("HUB_URL must be an http(s) URL")

        raw_token = env.get("HUB_TOKEN")
        if not raw_token or not raw_token.strip():
            raise ConfigurationError("HUB_TOKEN must be set")
        token = raw_token.strip()

        raw_agent_name = env.get("AGENT_NAME")
        if not raw_agent_name or not raw_agent_name.strip():
            raise ConfigurationError("AGENT_NAME must be set")
        agent_name = raw_agent_name.strip()

        default_wait_s = _positive_seconds(env, "HUB_DEFAULT_WAIT_S", DEFAULT_WAIT_S)
        heartbeat_s = _positive_seconds(env, "HUB_HEARTBEAT_S", DEFAULT_HEARTBEAT_S)
        max_retries = _non_negative_int(env, "HUB_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        backoff_factor_s = _positive_seconds(env, "HUB_BACKOFF_FACTOR_S", DEFAULT_BACKOFF_FACTOR_S)

        return cls(
            hub_url=hub_url,
            token=token,
            agent_name=agent_name,
            profile=profile_from_env(env),
            default_wait_s=default_wait_s,
            heartbeat_s=heartbeat_s,
            max_retries=max_retries,
            backoff_factor_s=backoff_factor_s,
        )
