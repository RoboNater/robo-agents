from typing import Any
from uuid import uuid4

import httpx
import pytest
from agent_hub.database import database
from agent_hub.store import HubStore
from agent_hub_common import (
    SCHEMA_VERSION,
    EventKind,
    Finding,
    ImplementerOutcome,
    ImplementerResult,
    MetaKeys,
    ReviewerResult,
    ReviewerVerdict,
    TaskState,
    TestResult,
)


def _rpc(
    method: str,
    params: dict[str, Any],
    req_id: str | None = None,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id or uuid4().hex,
        "method": method,
        "params": params,
    }


async def test_check_in_idempotency_and_conflict(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    op_id = "op-checkin-test-1"
    payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-1",
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.AGENT: "bob",
                    MetaKeys.CAPABILITIES: ["python"],
                    MetaKeys.RUNTIME: "claude-code",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                },
            }
        },
    )

    # First call: succeeds
    resp1 = await client.post("/a2a", json=payload)
    assert resp1.status_code == 200
    res1 = resp1.json()["result"]
    assert res1.get("contextId") is not None

    # Verify 1 event in database
    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.AGENT_CHECKED_IN.value,),
        ).fetchone()
        assert events["c"] == 1

    # Replay identical call: should return cached result without duplicate events
    resp2 = await client.post("/a2a", json=payload)
    assert resp2.status_code == 200
    res2 = resp2.json()["result"]
    assert res2 == res1

    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.AGENT_CHECKED_IN.value,),
        ).fetchone()
        assert events["c"] == 1

    # Conflicting call: same operation_id but different capabilities
    conflicting_payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-2",
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.AGENT: "bob",
                    MetaKeys.CAPABILITIES: ["go", "rust"],
                    MetaKeys.RUNTIME: "claude-code",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                },
            }
        },
    )
    resp3 = await client.post("/a2a", json=conflicting_payload)
    assert resp3.status_code == 409
    err3 = resp3.json()["error"]
    assert err3["code"] == -32600
    assert "already executed" in err3["message"]


async def test_progress_idempotency_and_conflict(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    # Check in bob
    hub_store.check_in("bob", ["python"], runtime="claude-code")
    agent = hub_store.agent_by_name("bob")
    assert agent is not None
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    op_id = "op-progress-test-1"
    payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-p1",
                "taskId": task.id,
                "contextId": agent.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "Starting tests"}],
                "metadata": {
                    MetaKeys.KIND: "progress",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                },
            }
        },
    )

    # First call: succeeds
    resp1 = await client.post("/a2a", json=payload)
    assert resp1.status_code == 200

    # Verify event count in database
    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.TASK_PROGRESS.value,),
        ).fetchone()
        assert events["c"] == 1

    # Replay identical call: returns cached result, no extra event
    resp2 = await client.post("/a2a", json=payload)
    assert resp2.status_code == 200

    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.TASK_PROGRESS.value,),
        ).fetchone()
        assert events["c"] == 1

    # Conflicting call: same operation_id but different note
    conflicting_payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-p2",
                "taskId": task.id,
                "contextId": agent.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "Different progress note"}],
                "metadata": {
                    MetaKeys.KIND: "progress",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                },
            }
        },
    )
    resp3 = await client.post("/a2a", json=conflicting_payload)
    assert resp3.status_code == 409
    err3 = resp3.json()["error"]
    assert err3["code"] == -32600
    assert "already executed" in err3["message"]


async def test_result_idempotency_and_conflict(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    hub_store.check_in("bob", ["python"], runtime="claude-code")
    agent = hub_store.agent_by_name("bob")
    assert agent is not None
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    op_id = "op-result-test-1"
    result_dict = {
        "outcome": "completed",
        "summary": "Done!",
        "pr_url": "https://github.com/org/repo/pull/1",
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
    }
    payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-r1",
                "taskId": task.id,
                "contextId": agent.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "Done!"}],
                "metadata": {
                    MetaKeys.KIND: "result",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                    MetaKeys.RESULT: result_dict,
                },
            }
        },
    )

    # First call: succeeds
    resp1 = await client.post("/a2a", json=payload)
    assert resp1.status_code == 200

    stored = hub_store.get_task(task.id)
    assert stored is not None and stored.state == TaskState.COMPLETED

    # Verify event count
    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.TASK_COMPLETED.value,),
        ).fetchone()
        assert events["c"] == 1

    # Replay identical call: returns cached result, no duplicate event
    resp2 = await client.post("/a2a", json=payload)
    assert resp2.status_code == 200

    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.TASK_COMPLETED.value,),
        ).fetchone()
        assert events["c"] == 1

    # Conflicting call: same operation_id but different summary
    conflicting_result = dict(result_dict, summary="Different summary")
    conflicting_payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-r2",
                "taskId": task.id,
                "contextId": agent.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "Different summary"}],
                "metadata": {
                    MetaKeys.KIND: "result",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                    MetaKeys.RESULT: conflicting_result,
                },
            }
        },
    )
    resp3 = await client.post("/a2a", json=conflicting_payload)
    assert resp3.status_code == 409
    err3 = resp3.json()["error"]
    assert err3["code"] == -32600
    assert "already executed" in err3["message"]


async def test_validation_failure_leaves_task_working(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    # 1. Reviewer validation failures
    hub_store.check_in("charlie", ["python"], runtime="codex")
    agent = hub_store.agent_by_name("charlie")
    assert agent is not None
    rev_task = hub_store.assign_task("charlie", "reviewer", "Review PR", "Review instructions")

    # Approved without reviewed_head_sha -> HTTP 400
    bad_reviewer_1 = {
        "verdict": "approved",
        "summary": "Looks good without sha",
    }
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": rev_task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "approved"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: bad_reviewer_1,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "requires reviewed_head_sha" in resp.json()["error"]["message"]

    # Task remains open (not completed or failed)
    task_after = hub_store.get_task(rev_task.id)
    assert task_after is not None and task_after.state not in (
        TaskState.COMPLETED,
        TaskState.FAILED,
    )

    # Approved with blocking findings -> HTTP 400
    bad_reviewer_2 = {
        "verdict": "approved",
        "summary": "Approved with blocker",
        "reviewed_head_sha": "0123456789abcdef0123456789abcdef01234567",
        "blocking_findings": [
            {
                "id": "r1-1",
                "text": "Critical bug in main loop",
            }
        ],
    }
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": rev_task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "approved"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: bad_reviewer_2,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "blocking_findings to be empty" in resp.json()["error"]["message"]

    # 2. Implementer validation failure
    hub_store.check_in("bob", ["python"], runtime="claude-code")
    bob_agent = hub_store.agent_by_name("bob")
    assert bob_agent is not None
    imp_task = hub_store.assign_task("bob", "implementer", "Fix bug", "Fix instructions")

    bad_implementer = {
        "outcome": "completed",
        "summary": "Completed without PR URL",
    }
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": imp_task.id,
                    "contextId": bob_agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "done"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: bad_implementer,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "requires pr_url" in resp.json()["error"]["message"]

    # Correct implementer submission succeeds
    good_implementer = {
        "outcome": "completed",
        "summary": "Completed with PR",
        "pr_url": "https://github.com/org/repo/pull/42",
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
    }
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": imp_task.id,
                    "contextId": bob_agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "done"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: good_implementer,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 200
    task_after = hub_store.get_task(imp_task.id)
    assert task_after is not None and task_after.state == TaskState.COMPLETED


@pytest.mark.parametrize(
    "bad_version",
    [
        2,
        -1,
        0,
        "1",
        True,
        False,
        None,
    ],
)
async def test_schema_version_rejections(
    client: httpx.AsyncClient, bad_version: Any
) -> None:
    # Check that invalid schema_version (when provided) is rejected with 400
    if bad_version is None:
        # None or omitted is valid for backwards compatibility
        return

    payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.AGENT: "bob",
                    MetaKeys.CAPABILITIES: ["python"],
                    MetaKeys.SCHEMA_VERSION: bad_version,
                    MetaKeys.OPERATION_ID: uuid4().hex,
                },
            }
        },
    )
    resp = await client.post("/a2a", json=payload)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == -32602
    assert "unsupported schema_version" in err["message"]


async def test_typed_result_roundtrip_a2a_to_get_state(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    hub_store.check_in("bob", ["python"], runtime="claude-code")
    agent = hub_store.agent_by_name("bob")
    assert agent is not None
    task = hub_store.assign_task("bob", "implementer", "Task Implementer", "Inst")

    imp_result = ImplementerResult(
        outcome=ImplementerOutcome.COMPLETED,
        summary="All green",
        pr_url="https://github.com/org/repo/pull/77",
        head_sha="0123456789abcdef0123456789abcdef01234567",
        commits=["0123456789abcdef0123456789abcdef01234567"],
        tests=[
            TestResult(command="pytest tests/test_models.py", status="passed"),
            TestResult(command="pytest tests/test_protocol.py", status="passed"),
        ],
        resolved_finding_ids=["r1-1", "r1-2"],
    )

    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "All green"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: imp_result.model_dump(mode="json"),
                    },
                }
            },
        ),
    )
    assert resp.status_code == 200

    # 1. Verify in database
    task_row = hub_store.get_task(task.id)
    assert task_row is not None
    assert task_row.result is not None
    assert task_row.result["pr_url"] == "https://github.com/org/repo/pull/77"
    assert task_row.result["resolved_finding_ids"] == ["r1-1", "r1-2"]
    assert len(task_row.result["tests"]) == 2
    assert task_row.result["tests"][0]["command"] == "pytest tests/test_models.py"

    # 2. Verify in get_state
    state = hub_store.get_state()
    tasks = state.get("tasks", [])
    matching = [t for t in tasks if t["id"] == task.id]
    assert len(matching) == 1
    assert matching[0]["result"]["head_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert matching[0]["result"]["resolved_finding_ids"] == ["r1-1", "r1-2"]

    # 3. Test ReviewerResult with findings
    hub_store.check_in("charlie", ["python"], runtime="codex")
    charlie_agent = hub_store.agent_by_name("charlie")
    assert charlie_agent is not None
    rev_task = hub_store.assign_task("charlie", "reviewer", "Task Review", "Inst")

    rev_result = ReviewerResult(
        verdict=ReviewerVerdict.CHANGES_REQUESTED,
        summary="Changes needed in parser",
        blocking_findings=[
            Finding(
                id="r2-1",
                text="Unchecked None dereference in parser.py:42",
            ),
        ],
        nonblocking_findings=[
            Finding(
                id="r2-2",
                text="Typo in README.md",
            ),
        ],
    )

    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": rev_task.id,
                    "contextId": charlie_agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Changes requested"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: rev_result.model_dump(mode="json"),
                    },
                }
            },
        ),
    )
    assert resp.status_code == 200

    rev_row = hub_store.get_task(rev_task.id)
    assert rev_row is not None
    assert rev_row.result is not None
    assert rev_row.result["verdict"] == "changes_requested"
    assert len(rev_row.result["blocking_findings"]) == 1
    assert rev_row.result["blocking_findings"][0]["id"] == "r2-1"
    assert len(rev_row.result["nonblocking_findings"]) == 1
    assert rev_row.result["nonblocking_findings"][0]["id"] == "r2-2"
