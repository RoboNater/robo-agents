from datetime import datetime

from agent_hub_common import utcnow_iso


def test_utcnow_iso_is_timezone_aware_and_sortable() -> None:
    first = utcnow_iso()
    second = utcnow_iso()

    assert first.endswith("Z")
    offset = datetime.fromisoformat(first).utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    assert first <= second
