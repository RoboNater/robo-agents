import json
import re
from pathlib import Path
from typing import Any

import pytest
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    JSONRPCSuccessResponse,
    Message,
    SendMessageRequest,
    SendStreamingMessageRequest,
    Task,
)
from agent_hub.database import initialize_database
from agent_hub.protocol import A2AProtocol
from agent_hub.store import HubStore
from agent_hub_common import HubSettings, MetaKeys

WIRE_DIR = Path(__file__).parent / "wire"


def _collect_metadata_keys(obj: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "metadata" and isinstance(v, dict):
                keys.extend(v.keys())
            keys.extend(_collect_metadata_keys(v))
    elif isinstance(obj, list):
        for item in obj:
            keys.extend(_collect_metadata_keys(item))
    return keys


@pytest.mark.parametrize(
    "fixture_name",
    [
        "message_send.json",
        "message_stream.json",
        "tasks_get.json",
        "tasks_cancel.json",
    ],
)
def test_wire_fixtures_round_trip_a2a_sdk_models(fixture_name: str) -> None:
    fixture_path = WIRE_DIR / fixture_name
    assert fixture_path.exists(), f"Fixture {fixture_name} missing"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    method = data["method"]

    # Validate request against a2a-sdk model and assert wire shape round-trips
    validated_req: (
        SendMessageRequest
        | SendStreamingMessageRequest
        | GetTaskRequest
        | CancelTaskRequest
    )
    if method == "message/send":
        validated_req = SendMessageRequest.model_validate(data["request"])
    elif method == "message/stream":
        validated_req = SendStreamingMessageRequest.model_validate(data["request"])
    elif method == "tasks/get":
        validated_req = GetTaskRequest.model_validate(data["request"])
    elif method == "tasks/cancel":
        validated_req = CancelTaskRequest.model_validate(data["request"])
    else:
        raise ValueError(f"Unknown method: {method}")

    dumped_req = validated_req.model_dump(mode="json", exclude_none=True, by_alias=True)
    assert dumped_req == data["request"]

    # Validate response against a2a-sdk model and assert wire shape round-trips
    validated_resp = JSONRPCSuccessResponse.model_validate(data["response"])
    if method == "message/send":
        msg_result = Message.model_validate(data["response"]["result"])
        dumped_result = msg_result.model_dump(
            mode="json", exclude_none=True, by_alias=True
        )
        assert dumped_result == data["response"]["result"]
    else:
        task_result = Task.model_validate(data["response"]["result"])
        dumped_result = task_result.model_dump(
            mode="json", exclude_none=True, by_alias=True
        )
        assert dumped_result == data["response"]["result"]
    dumped_resp = validated_resp.model_dump(mode="json", exclude_none=True, by_alias=True)
    assert dumped_resp == data["response"]


def test_no_unprefixed_hub_keys_in_fixtures() -> None:
    for fixture_path in WIRE_DIR.glob("*.json"):
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        keys = _collect_metadata_keys(data)
        assert keys, f"Expected metadata keys in {fixture_path.name}"
        for key in keys:
            assert key.startswith("hub."), f"Found unprefixed key {key!r} in {fixture_path.name}"


def test_no_unprefixed_hub_keys_in_packages_code() -> None:
    import ast

    packages_dir = Path(__file__).resolve().parents[1] / "packages"
    meta_get_re = re.compile(r'metadata\.get\(\s*["\']([^"\']+)["\']')
    meta_sub_re = re.compile(r'metadata\[\s*["\']([^"\']+)["\']\s*\]')

    for py_file in packages_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for match in meta_get_re.finditer(text):
            key = match.group(1)
            assert key.startswith("hub."), (
                f"Unprefixed metadata.get key {key!r} in {py_file}"
            )
        for match in meta_sub_re.finditer(text):
            key = match.group(1)
            assert key.startswith("hub."), (
                f"Unprefixed metadata subscript key {key!r} in {py_file}"
            )

        tree = ast.parse(text, filename=str(py_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "metadata"
                and isinstance(node.value, ast.Dict)
            ):
                for key_node in node.value.keys:
                    if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                        assert key_node.value.startswith("hub."), (
                            f"{py_file}: Unprefixed key {key_node.value!r} in metadata literal"
                        )
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "metadata":
                        for key_node in node.value.keys:
                            if isinstance(key_node, ast.Constant) and isinstance(
                                key_node.value, str
                            ):
                                msg = f"{py_file}: Unprefixed key {key_node.value!r} in metadata"
                                assert key_node.value.startswith("hub."), msg


async def test_wire_fixtures_dispatch(tmp_path: Path) -> None:
    db_path = tmp_path / "wire_hub.db"
    initialize_database(db_path)
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=db_path,
        token="wire-token",
        token_file=tmp_path / "token",
        guides_dir=tmp_path / "guides",
    )
    store = HubStore(db_path)
    protocol = A2AProtocol(store, settings)

    # 1. message/send check-in fixture
    send_fixture = json.loads((WIRE_DIR / "message_send.json").read_text(encoding="utf-8"))
    resp = await protocol.dispatch(send_fixture["request"])
    resp_data = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp_data["id"] == send_fixture["request"]["id"]
    assert resp_data["result"]["kind"] == "message"
    assert resp_data["result"]["metadata"][MetaKeys.AGENT] == "bob"
    assert resp_data["result"]["metadata"][MetaKeys.KIND] == "check_in_ack"
    assert resp_data["result"]["metadata"][MetaKeys.STATUS] == "idle"

    # Assign task so tasks/get and tasks/cancel work
    task = store.assign_task("bob", "implementer", "Fix #1", "Fix issue #1")

    # 2. tasks/get fixture
    get_fixture = json.loads((WIRE_DIR / "tasks_get.json").read_text(encoding="utf-8"))
    get_req = dict(get_fixture["request"])
    get_req["params"] = {"id": task.id, "historyLength": 10}
    resp_get = await protocol.dispatch(get_req)
    get_data = json.loads(bytes(resp_get.body).decode("utf-8"))
    assert get_data["result"]["id"] == task.id
    assert get_data["result"]["kind"] == "task"
    assert get_data["result"]["metadata"][MetaKeys.ROLE] == "implementer"
    assert get_data["result"]["metadata"][MetaKeys.ASSIGNEE] == "bob"

    # 3. tasks/cancel fixture
    cancel_fixture = json.loads((WIRE_DIR / "tasks_cancel.json").read_text(encoding="utf-8"))
    cancel_req = dict(cancel_fixture["request"])
    cancel_req["params"] = {"id": task.id}
    resp_cancel = await protocol.dispatch(cancel_req)
    cancel_data = json.loads(bytes(resp_cancel.body).decode("utf-8"))
    assert cancel_data["result"]["id"] == task.id
    assert cancel_data["result"]["status"]["state"] == "canceled"
    assert cancel_data["result"]["metadata"][MetaKeys.ASSIGNEE] == "bob"
