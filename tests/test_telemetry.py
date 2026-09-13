import json
from pathlib import Path

from worker_mcp.telemetry import TelemetryLog


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_telemetry_appends_bounded_structured_events(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "worker.jsonl"
    telemetry = TelemetryLog(
        path,
        agent="bob",
        worker_instance_id="worker-1",
        session_fields={"harness": "codex", "model": "gpt-test"},
    )

    call_id, started = telemetry.start_tool("await_assignment")
    telemetry.finish_tool(
        "await_assignment", call_id, started, result={"timeout": True, "instructions": "secret"}
    )
    telemetry.emit(
        "retry",
        operation="message/stream",
        attempt=1,
        max_retries=3,
        reason="ReadTimeout",
        delay_s=0.5,
    )

    records = _records(path)
    assert [record["event"] for record in records] == [
        "session_started",
        "tool_call",
        "tool_call",
        "retry",
    ]
    assert records[0]["harness"] == "codex"
    assert records[0]["model"] == "gpt-test"
    assert records[2]["phase"] == "success"
    assert records[2]["outcome"] == "timeout"
    assert "instructions" not in records[2]
    assert all(record["timestamp"] for record in records)
    assert len({record["session_id"] for record in records}) == 1


def test_telemetry_records_tool_errors_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "worker.jsonl"
    telemetry = TelemetryLog(path, agent="bob", worker_instance_id="worker-1")
    call_id, started = telemetry.start_tool("get_role_guide")
    telemetry.finish_tool(
        "get_role_guide",
        call_id,
        started,
        error=RuntimeError("guide unavailable"),
    )

    error = _records(path)[-1]
    assert error["phase"] == "error"
    assert error["error_type"] == "RuntimeError"
    assert error["error"] == "guide unavailable"


def test_telemetry_directory_failure_does_not_break_worker(tmp_path: Path) -> None:
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("occupied", encoding="utf-8")

    telemetry = TelemetryLog(
        blocking_file / "worker.jsonl",
        agent="bob",
        worker_instance_id="worker-1",
    )

    assert telemetry.path is None
