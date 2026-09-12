"""Structured, append-only telemetry for worker endurance runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TelemetryLog:
    """Write one bounded JSON object per line without ever touching stdout.

    The file is opened for each event so a thin supervisor can restart the MCP
    process and keep appending to the same run log. Telemetry failures are
    reported on stderr and never break the worker protocol.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        agent: str,
        worker_instance_id: str,
        session_fields: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.agent = agent
        self.worker_instance_id = worker_instance_id
        self.session_id = uuid4().hex
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.emit("session_started", **dict(session_fields or {}))

    def emit(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {
            "timestamp": _timestamp(),
            "event": event,
            "agent": self.agent,
            "worker_instance_id": self.worker_instance_id,
            "session_id": self.session_id,
            **fields,
        }
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except OSError:
            logger.exception("Could not append worker telemetry to %s", self.path)

    def start_tool(self, tool: str) -> tuple[str, float]:
        call_id = uuid4().hex
        self.emit("tool_call", phase="start", tool=tool, call_id=call_id)
        return call_id, monotonic()

    def finish_tool(
        self,
        tool: str,
        call_id: str,
        started: float,
        *,
        result: Any | None = None,
        error: BaseException | None = None,
    ) -> None:
        common = {
            "tool": tool,
            "call_id": call_id,
            "duration_s": round(monotonic() - started, 3),
        }
        if error is not None:
            self.emit(
                "tool_call",
                phase="error",
                error_type=type(error).__name__,
                error=str(error)[:500],
                **common,
            )
            return
        self.emit(
            "tool_call",
            phase="success",
            outcome=_tool_outcome(result),
            **common,
        )


def _tool_outcome(result: Any) -> str:
    """Summarize a tool result without copying external text into telemetry."""

    if not isinstance(result, dict):
        return "success"
    for marker in ("timeout", "release", "task_ended", "ok"):
        if result.get(marker) is True:
            return marker
    if result.get("task_id") and result.get("role"):
        return "assignment"
    if "reply" in result:
        return "reply"
    status = result.get("status")
    return str(status) if status else "success"
