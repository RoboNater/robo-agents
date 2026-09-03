"""Consistent timestamps for persisted hub state."""

from datetime import UTC, datetime


def utcnow_iso() -> str:
    """Return a lexically sortable, timezone-aware UTC timestamp."""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
