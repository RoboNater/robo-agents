"""The A2A JSON-RPC surface workers speak (spec §4.1).

Workers are A2A clients, so every pull-model intent has to ride on a standard
method. `message/send` carries the intents that return immediately; the two
that wait — get an assignment, ask Alice a question — use `message/stream`, and
the hub holds the SSE response open until the answer exists or the bounded
deadline passes. `tasks/get` and `tasks/cancel` are there for debugging.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from a2a.types import (
    Artifact,
    CancelTaskRequest,
    GetTaskRequest,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    JSONParseError,
    JSONRPCError,
    JSONRPCErrorResponse,
    JSONRPCSuccessResponse,
    MessageSendParams,
    MethodNotFoundError,
    Part,
    Role,
    SendMessageRequest,
    SendStreamingMessageRequest,
    Task,
    TaskIdParams,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskQueryParams,
    TaskStatus,
    TextPart,
    UnsupportedOperationError,
)
from a2a.types import Message as A2AMessage
from a2a.types import TaskState as A2ATaskState
from agent_hub_common import (
    HubSettings,
    ImplementerOutcome,
    ImplementerResult,
    MetaKeys,
    ReviewerResult,
    ReviewerVerdict,
    TaskResult,
    TaskState,
)
from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .store import (
    AgentRecord,
    ConflictError,
    HubStore,
    IdempotencyConflictError,
    MessageRecord,
    NotFoundError,
    Released,
    TaskRecord,
    _normalize_part,
)

logger = logging.getLogger(__name__)

CHECK_IN_TEXT = "READY"
NEXT_TEXT = "NEXT"
SSE_MEDIA_TYPE = "text/event-stream"
# Proxies that buffer would defeat the point of holding the response open.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

SUPPORTED_METHODS = frozenset({"message/send", "message/stream", "tasks/get", "tasks/cancel"})

RequestId = str | int | None
A2AErrorModel = (
    JSONRPCError
    | JSONParseError
    | InvalidParamsError
    | InvalidRequestError
    | MethodNotFoundError
    | TaskNotFoundError
    | TaskNotCancelableError
    | UnsupportedOperationError
    | InternalError
)


class ProtocolError(Exception):
    """A refusal that must be reported as a JSON-RPC error, not an exception."""

    def __init__(self, error: A2AErrorModel) -> None:
        super().__init__(error.message)
        self.error = error


class ResultValidationError(ProtocolError):
    """Raised when a typed task result fails validation."""

    def __init__(self, message: str) -> None:
        super().__init__(InvalidParamsError(message=message))


class UnsupportedSchemaVersionError(ProtocolError):
    """Raised when an unsupported hub.schema_version is encountered."""

    def __init__(self, version: Any) -> None:
        super().__init__(InvalidParamsError(message=f"unsupported schema_version: {version!r}"))


def _invalid(message: str) -> ProtocolError:
    return ProtocolError(InvalidParamsError(message=message))


def _check_schema_version(metadata: Mapping[str, Any]) -> None:
    if MetaKeys.SCHEMA_VERSION in metadata:
        version = metadata[MetaKeys.SCHEMA_VERSION]
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            raise UnsupportedSchemaVersionError(version)


def _hash_payload(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _error_response(
    request_id: RequestId,
    error: A2AErrorModel,
    status_code: int = 200,
) -> JSONResponse:
    body = JSONRPCErrorResponse(id=request_id, error=error)
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=status_code)


def parse_error_response() -> JSONResponse:
    """Report a body that is not JSON at all, per JSON-RPC."""

    return _error_response(None, JSONParseError())


def _success_body(request_id: RequestId, result: Task | A2AMessage) -> dict[str, Any]:
    body = JSONRPCSuccessResponse(id=request_id, result=result)
    return body.model_dump(mode="json", exclude_none=True)


def _sse(chunk: Mapping[str, Any]) -> bytes:
    return f"data: {json.dumps(chunk)}\n\n".encode()


def _text(message: A2AMessage) -> str:
    """Join the message's text parts; non-text parts carry no worker intent."""

    return "".join(
        part.root.text for part in message.parts if isinstance(part.root, TextPart)
    ).strip()


def _metadata(message: A2AMessage) -> dict[str, Any]:
    return dict(message.metadata or {})


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _invalid(f"metadata.{field} must be a list of strings")
    return [str(item) for item in value]


def _agent_message(
    text: str,
    *,
    context_id: str | None = None,
    task_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> A2AMessage:
    """Build a message from the hub back to a worker."""

    return A2AMessage(
        message_id=uuid4().hex,
        role=Role.agent,
        parts=[Part(root=TextPart(text=text))],
        context_id=context_id,
        task_id=task_id,
        metadata=dict(metadata) if metadata else None,
    )


def _stored_message(record: MessageRecord) -> A2AMessage:
    return A2AMessage(
        message_id=str(record.id),
        role=Role.user if record.direction == "to_alice" else Role.agent,
        parts=[Part.model_validate(_normalize_part(part)) for part in record.parts],
        context_id=record.context_id,
        task_id=record.task_id,
        metadata={MetaKeys.SENDER: record.sender, MetaKeys.TS: record.ts},
    )


def _artifacts(record: TaskRecord) -> list[Artifact] | None:
    """Expose a worker's reported artifacts — PR URL, SHAs — on the task."""

    result = record.result or {}
    artifacts = []
    reported = result.get("artifacts")
    if isinstance(reported, list):
        for item in reported:
            payload = item if isinstance(item, dict) else {"value": item}
            name = payload.get("name")
            artifacts.append(
                Artifact(
                    artifact_id=uuid4().hex,
                    name=str(name) if isinstance(name, str) else None,
                    parts=[Part(root=TextPart(text=json.dumps(payload, sort_keys=True)))],
                )
            )
    for field in ("pr_url", "head_sha", "reviewed_head_sha", "review_url"):
        val = result.get(field)
        if isinstance(val, str) and val:
            artifacts.append(
                Artifact(
                    artifact_id=uuid4().hex,
                    name=field,
                    parts=[Part(root=TextPart(text=val))],
                )
            )
    return artifacts or None


def _task_object(
    record: TaskRecord,
    context_id: str,
    history: list[A2AMessage] | None = None,
    status_message: A2AMessage | None = None,
) -> Task:
    """Render a stored task as the A2A Task a worker or debugger receives."""

    if status_message is None:
        status_message = _agent_message(
            record.instructions,
            context_id=context_id,
            task_id=record.id,
            metadata={
                MetaKeys.KIND: "assignment",
                MetaKeys.ROLE: record.role,
                MetaKeys.TITLE: record.title,
            },
        )
    return Task(
        id=record.id,
        context_id=context_id,
        status=TaskStatus(
            state=A2ATaskState(record.state.value),
            timestamp=record.updated,
            message=status_message,
        ),
        history=history,
        artifacts=_artifacts(record),
        metadata={
            MetaKeys.ROLE: record.role,
            MetaKeys.TITLE: record.title,
            MetaKeys.ASSIGNEE: record.assignee,
            MetaKeys.LEASE_EXPIRES: record.lease_expires,
            MetaKeys.RESULT: record.result,
        },
    )


@dataclass(frozen=True, slots=True)
class A2AProtocol:
    """Maps A2A calls onto hub state. Holds no state of its own."""

    store: HubStore
    settings: HubSettings

    async def dispatch(self, payload: Any) -> Response:
        """Route one JSON-RPC request, turning refusals into error responses."""

        if not isinstance(payload, dict):
            return _error_response(None, InvalidRequestError())
        request_id = payload.get("id")
        request_id = request_id if isinstance(request_id, str | int) else None
        method = payload.get("method")
        if not isinstance(method, str):
            return _error_response(request_id, InvalidRequestError())
        if method not in SUPPORTED_METHODS:
            return _error_response(request_id, MethodNotFoundError())

        try:
            return await self._dispatch(method, payload, request_id)
        except ResultValidationError as exc:
            return _error_response(request_id, exc.error, status_code=400)
        except UnsupportedSchemaVersionError as exc:
            return _error_response(request_id, exc.error, status_code=400)
        except IdempotencyConflictError as exc:
            return _error_response(
                request_id, InvalidRequestError(message=str(exc)), status_code=409
            )
        except ProtocolError as exc:
            return _error_response(request_id, exc.error)
        except NotFoundError as exc:
            return _error_response(request_id, InvalidParamsError(message=str(exc)))
        except ConflictError as exc:
            return _error_response(request_id, InvalidRequestError(message=str(exc)))
        except Exception:
            logger.exception("Unhandled error serving %s", method)
            return _error_response(request_id, InternalError())

    async def _dispatch(self, method: str, payload: Any, request_id: RequestId) -> Response:
        if method == "message/send":
            params = _validate(SendMessageRequest, payload).params
            return JSONResponse(_success_body(request_id, self._send(params)))
        if method == "message/stream":
            params = _validate(SendStreamingMessageRequest, payload).params
            return self._stream(request_id, params)
        if method == "tasks/get":
            query = _validate(GetTaskRequest, payload).params
            return JSONResponse(_success_body(request_id, self._get_task(query)))
        cancel = _validate(CancelTaskRequest, payload).params
        return JSONResponse(_success_body(request_id, self._cancel_task(cancel)))

    # -- message/send -------------------------------------------------------

    def _send(self, params: MessageSendParams) -> Task | A2AMessage:
        message = params.message
        metadata = _metadata(message)
        if message.task_id is None:
            return self._check_in(message, metadata)

        agent = self._resolve_agent(message, metadata)
        task = self._owned_task(message.task_id, agent)
        kind = metadata.get(MetaKeys.KIND, "progress")
        if kind == "result":
            return self._result(task, agent, _text(message), metadata)
        if kind == "progress":
            _check_schema_version(metadata)
            operation_id = metadata.get(MetaKeys.OPERATION_ID)
            payload_hash: str | None = None
            note = _text(message)
            if operation_id is not None:
                if not isinstance(operation_id, str) or not operation_id.strip():
                    raise _invalid(f"metadata.{MetaKeys.OPERATION_ID} must be a non-empty string")
                operation_id = operation_id.strip()
                payload_hash = _hash_payload({
                    "intent": "progress",
                    "task_id": task.id,
                    "note": note,
                })
                existing = self.store.get_operation(agent.name, operation_id)
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise IdempotencyConflictError(
                            f"operation {operation_id!r} already executed with different payload"
                        )
                    return A2AMessage.model_validate(json.loads(existing["response_json"]))

            self.store.record_progress(task.id, agent.name, note)
            resp = _agent_message(
                "noted",
                context_id=agent.context_id,
                task_id=task.id,
                metadata={MetaKeys.KIND: "progress_ack"},
            )
            if operation_id is not None and payload_hash is not None:
                self.store.record_operation(
                    agent.name,
                    operation_id,
                    payload_hash,
                    json.dumps(resp.model_dump(mode="json", exclude_none=True)),
                )
            return resp
        raise _invalid(f"metadata.{MetaKeys.KIND} {kind!r} is not a message/send intent on a task")

    def _check_in(self, message: A2AMessage, metadata: dict[str, Any]) -> A2AMessage:
        if _text(message).upper() != CHECK_IN_TEXT:
            raise _invalid(f"a message with no taskId must be the {CHECK_IN_TEXT} check-in")
        _check_schema_version(metadata)
        name = metadata.get(MetaKeys.AGENT)
        if not isinstance(name, str) or not name.strip():
            raise _invalid(f"check-in requires metadata.{MetaKeys.AGENT}")
        agent_name = name.strip()
        runtime = metadata.get(MetaKeys.RUNTIME)
        capabilities = _string_list(metadata.get(MetaKeys.CAPABILITIES), MetaKeys.CAPABILITIES)

        operation_id = metadata.get(MetaKeys.OPERATION_ID)
        payload_hash: str | None = None
        if operation_id is not None:
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise _invalid(f"metadata.{MetaKeys.OPERATION_ID} must be a non-empty string")
            operation_id = operation_id.strip()
            payload_hash = _hash_payload({
                "intent": "check_in",
                "agent": agent_name,
                "capabilities": sorted(capabilities),
                "runtime": str(runtime) if runtime is not None else None,
            })
            existing = self.store.get_operation(agent_name, operation_id)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflictError(
                        f"operation {operation_id!r} already executed with different payload"
                    )
                return A2AMessage.model_validate(json.loads(existing["response_json"]))

        agent = self.store.check_in(
            agent_name,
            capabilities,
            runtime=str(runtime) if isinstance(runtime, str) else None,
        )
        resp = _agent_message(
            "REGISTERED",
            context_id=agent.context_id,
            metadata={
                MetaKeys.KIND: "check_in_ack",
                MetaKeys.AGENT: agent.name,
                MetaKeys.STATUS: agent.status.value,
            },
        )
        if operation_id is not None and payload_hash is not None:
            self.store.record_operation(
                agent_name,
                operation_id,
                payload_hash,
                json.dumps(resp.model_dump(mode="json", exclude_none=True)),
            )
        return resp

    def _result(
        self,
        task: TaskRecord,
        agent: AgentRecord,
        summary: str,
        metadata: dict[str, Any],
    ) -> Task:
        _check_schema_version(metadata)
        operation_id = metadata.get(MetaKeys.OPERATION_ID)
        if operation_id is not None:
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise _invalid(f"metadata.{MetaKeys.OPERATION_ID} must be a non-empty string")
            operation_id = operation_id.strip()

        raw_result = metadata.get(MetaKeys.RESULT)
        typed_result: TaskResult
        if raw_result is not None:
            if not isinstance(raw_result, dict):
                raise ResultValidationError(f"metadata.{MetaKeys.RESULT} must be an object")
            result_dict = dict(raw_result)
            if not result_dict.get("summary") and summary:
                result_dict["summary"] = summary
            try:
                if task.role == "implementer":
                    typed_result = ImplementerResult.model_validate(result_dict)
                elif task.role == "reviewer":
                    typed_result = ReviewerResult.model_validate(result_dict)
                elif "outcome" in result_dict:
                    typed_result = ImplementerResult.model_validate(result_dict)
                elif "verdict" in result_dict:
                    typed_result = ReviewerResult.model_validate(result_dict)
                else:
                    raise ValueError("result must specify 'outcome' or 'verdict'")
            except ValidationError as exc:
                first_err = exc.errors()[0]
                msg = first_err.get("msg", str(exc))
                loc = ".".join(str(part) for part in first_err.get("loc", []))
                raise ResultValidationError(f"{loc}: {msg}" if loc else msg) from exc
            except ValueError as exc:
                raise ResultValidationError(str(exc)) from exc
        else:
            raw_status = metadata.get(MetaKeys.STATUS)
            if raw_status is None:
                msg = f"metadata.{MetaKeys.STATUS} must be 'completed' or 'failed'"
                raise _invalid(msg)
            try:
                status = TaskState(str(raw_status))
                if status not in (TaskState.COMPLETED, TaskState.FAILED):
                    raise ValueError("not terminal")
            except ValueError as exc:
                msg = f"metadata.{MetaKeys.STATUS} must be 'completed' or 'failed'"
                raise _invalid(msg) from exc
            artifacts = metadata.get(MetaKeys.ARTIFACTS) or []
            if not isinstance(artifacts, list):
                raise _invalid(f"metadata.{MetaKeys.ARTIFACTS} must be a list")
            if task.role == "implementer":
                pr_url = None
                head_sha = None
                for a in artifacts:
                    if isinstance(a, dict):
                        if "url" in a:
                            pr_url = a["url"]
                        if "sha" in a:
                            head_sha = a["sha"]
                outcome = (
                    ImplementerOutcome.COMPLETED
                    if status is TaskState.COMPLETED
                    else ImplementerOutcome.FAILED
                )
                if outcome == ImplementerOutcome.COMPLETED and (not pr_url or not head_sha):
                    pr_url = pr_url or "https://github.com/unknown/pr"
                    head_sha = head_sha or "0000000"
                typed_result = ImplementerResult(
                    outcome=outcome,
                    pr_url=pr_url,
                    head_sha=head_sha,
                    summary=summary or "Legacy result",
                )
            elif task.role == "reviewer":
                verdict = (
                    ReviewerVerdict.APPROVED
                    if status is TaskState.COMPLETED
                    else ReviewerVerdict.FAILED
                )
                reviewed_head_sha = "0000000" if verdict == ReviewerVerdict.APPROVED else None
                typed_result = ReviewerResult(
                    verdict=verdict,
                    reviewed_head_sha=reviewed_head_sha,
                    summary=summary or "Legacy review result",
                )
            else:
                typed_result = ImplementerResult(
                    outcome=ImplementerOutcome.COMPLETED
                    if status is TaskState.COMPLETED
                    else ImplementerOutcome.FAILED,
                    pr_url="https://github.com/unknown/pr",
                    head_sha="0000000",
                    summary=summary,
                )

        payload_hash: str | None = None
        if operation_id is not None:
            payload_hash = _hash_payload({
                "intent": "result",
                "task_id": task.id,
                "result": typed_result.model_dump(mode="json"),
            })
            existing = self.store.get_operation(agent.name, operation_id)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflictError(
                        f"operation {operation_id!r} already executed with different payload"
                    )
                return Task.model_validate(json.loads(existing["response_json"]))

        finished = self.store.submit_result(task.id, agent.name, typed_result)
        task_obj = _task_object(finished, agent.context_id)
        if operation_id is not None and payload_hash is not None:
            self.store.record_operation(
                agent.name,
                operation_id,
                payload_hash,
                json.dumps(task_obj.model_dump(mode="json", exclude_none=True)),
            )
        return task_obj

    # -- message/stream -----------------------------------------------------

    def _stream(self, request_id: RequestId, params: MessageSendParams) -> Response:
        message = params.message
        metadata = _metadata(message)
        timeout_s = self._timeout(metadata)

        if message.task_id is not None:
            if metadata.get(MetaKeys.KIND) != "question":
                raise _invalid(
                    f"a streaming call on a task must be metadata.{MetaKeys.KIND}=question"
                )
            agent = self._resolve_agent(message, metadata)
            task = self._owned_task(message.task_id, agent)
            question = _text(message)
            if not question:
                raise _invalid("a question needs text")
            # The task is parked and Alice is notified before the response body
            # opens, so a client that disconnects still leaves the question with
            # her rather than losing it with the stream.
            question_id = self.store.open_question(
                task.id, agent.name, question, message.message_id
            )
            return self._streaming(
                self._reply_stream(
                    request_id, task, agent, question_id, timeout_s, message.message_id
                )
            )

        if _text(message).upper() != NEXT_TEXT:
            raise _invalid(f"a streaming call with no taskId must be the {NEXT_TEXT} poll")
        agent = self._resolve_agent(message, metadata)
        return self._streaming(self._assignment_stream(request_id, agent, timeout_s))

    def _streaming(self, body: AsyncIterator[bytes]) -> StreamingResponse:
        return StreamingResponse(body, media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)

    async def _assignment_stream(
        self, request_id: RequestId, agent: AgentRecord, timeout_s: float
    ) -> AsyncIterator[bytes]:
        outcome = await self.store.await_assignment(agent.context_id, timeout_s)
        if isinstance(outcome, Released):
            yield _sse(
                _success_body(
                    request_id,
                    _agent_message(
                        "RELEASED",
                        context_id=agent.context_id,
                        metadata={MetaKeys.KIND: "release", MetaKeys.RELEASE: True},
                    ),
                )
            )
            return
        if outcome is None:
            yield _sse(_success_body(request_id, self._timeout_message(agent.context_id)))
            return
        yield _sse(_success_body(request_id, _task_object(outcome, agent.context_id)))

    async def _reply_stream(
        self,
        request_id: RequestId,
        task: TaskRecord,
        agent: AgentRecord,
        question_id: int,
        timeout_s: float,
        sent_as: str,
    ) -> AsyncIterator[bytes]:
        reply = await self.store.await_reply(task.id, question_id, timeout_s)
        current = self.store.get_task(task.id)
        if current is not None and current.state in (
            TaskState.CANCELED,
            TaskState.FAILED,
            TaskState.COMPLETED,
        ):
            note = str((current.result or {}).get("summary", current.state.value))
            status_message = _agent_message(
                note,
                context_id=agent.context_id,
                task_id=current.id,
                metadata={MetaKeys.KIND: "state_override", MetaKeys.STATE: current.state.value},
            )
            yield _sse(
                _success_body(
                    request_id,
                    _task_object(current, agent.context_id, status_message=status_message),
                )
            )
            return
        if reply is None:
            yield _sse(
                _success_body(
                    request_id,
                    self._timeout_message(agent.context_id, task.id, sent_as),
                )
            )
            return
        yield _sse(_success_body(request_id, _stored_message(reply)))

    def _timeout_message(
        self, context_id: str, task_id: str | None = None, sent_as: str | None = None
    ) -> A2AMessage:
        """Tell the worker the hold elapsed; the guide says to call again.

        A retried question has to carry the message id it was first asked
        under, so the marker names it rather than leaving the caller to
        remember: an answer Alice gave in the gap is only reachable through the
        original question.
        """

        metadata: dict[str, Any] = {MetaKeys.KIND: "timeout", MetaKeys.TIMEOUT: True}
        if sent_as is not None:
            metadata[MetaKeys.RETRY_AS_MESSAGE_ID] = sent_as
        return _agent_message(
            "TIMEOUT",
            context_id=context_id,
            task_id=task_id,
            metadata=metadata,
        )

    def _timeout(self, metadata: Mapping[str, Any]) -> float:
        requested = metadata.get(MetaKeys.TIMEOUT_S)
        if requested is None:
            return self.settings.bounded_wait(None)
        if not isinstance(requested, int | float) or isinstance(requested, bool):
            raise _invalid(f"metadata.{MetaKeys.TIMEOUT_S} must be a number of seconds")
        return self.settings.bounded_wait(float(requested))

    # -- tasks/get and tasks/cancel ----------------------------------------

    def _get_task(self, params: TaskQueryParams) -> Task:
        record = self.store.get_task(params.id)
        if record is None:
            raise ProtocolError(TaskNotFoundError())
        history = [
            _stored_message(message)
            for message in self.store.task_history(params.id, params.history_length)
        ]
        return _task_object(record, self.store.task_context_id(record.id), history)

    def _cancel_task(self, params: TaskIdParams) -> Task:
        record = self.store.get_task(params.id)
        if record is None:
            raise ProtocolError(TaskNotFoundError())
        try:
            canceled = self.store.cancel_task(params.id)
        except ConflictError as exc:
            raise ProtocolError(TaskNotCancelableError(message=str(exc))) from exc
        return _task_object(canceled, self.store.task_context_id(canceled.id))

    # -- shared lookups -----------------------------------------------------

    def _resolve_agent(self, message: A2AMessage, metadata: Mapping[str, Any]) -> AgentRecord:
        """Identify the caller by its context id, falling back to its name."""

        if message.context_id:
            agent = self.store.agent_by_context(message.context_id)
            if agent is None:
                raise _invalid(f"unknown contextId {message.context_id!r}; check in first")
            return agent
        name = metadata.get(MetaKeys.AGENT)
        if isinstance(name, str) and name.strip():
            agent = self.store.agent_by_name(name.strip())
            if agent is None:
                raise _invalid(f"unknown agent {name!r}; check in first")
            return agent
        raise _invalid(f"the message needs a contextId or metadata.{MetaKeys.AGENT}")

    def _owned_task(self, task_id: str, agent: AgentRecord) -> TaskRecord:
        task = self.store.get_task(task_id)
        if task is None:
            raise ProtocolError(TaskNotFoundError())
        if task.assignee != agent.name:
            raise _invalid(f"task {task_id} is not assigned to {agent.name}")
        return task


def _validate(model: type[Any], payload: Any) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ProtocolError(InvalidParamsError(message=_first_error(exc))) from exc


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"{location}: {first['msg']}" if location else first["msg"]
