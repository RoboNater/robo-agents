"""The A2A JSON-RPC surface workers speak (spec §4.1).

Workers are A2A clients, so every pull-model intent has to ride on a standard
method. `message/send` carries the intents that return immediately; the two
that wait — get an assignment, ask Alice a question — use `message/stream`, and
the hub holds the SSE response open until the answer exists or the bounded
deadline passes. `tasks/get` and `tasks/cancel` are there for debugging.
"""

from __future__ import annotations

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
from agent_hub_common import HubSettings, TaskState
from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .store import (
    AgentRecord,
    ConflictError,
    HubStore,
    MessageRecord,
    NotFoundError,
    Released,
    TaskRecord,
)

logger = logging.getLogger(__name__)

CHECK_IN_TEXT = "READY"
NEXT_TEXT = "NEXT"
SSE_MEDIA_TYPE = "text/event-stream"
# Proxies that buffer would defeat the point of holding the response open.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

SUPPORTED_METHODS = frozenset(
    {"message/send", "message/stream", "tasks/get", "tasks/cancel"}
)

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


def _invalid(message: str) -> ProtocolError:
    return ProtocolError(InvalidParamsError(message=message))


def _error_response(request_id: RequestId, error: A2AErrorModel) -> JSONResponse:
    body = JSONRPCErrorResponse(id=request_id, error=error)
    # JSON-RPC transports protocol failures in the body; the HTTP status stays
    # 200 so a client reads one error shape. Authentication is the exception
    # and is refused with 401 before dispatch ever runs.
    return JSONResponse(body.model_dump(mode="json", exclude_none=True))


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
        parts=[Part.model_validate(part) for part in record.parts],
        context_id=record.context_id,
        task_id=record.task_id,
        metadata={"sender": record.sender, "ts": record.ts},
    )


def _artifacts(record: TaskRecord) -> list[Artifact] | None:
    """Expose a worker's reported artifacts — PR URL, SHAs — on the task."""

    result = record.result or {}
    reported = result.get("artifacts")
    if not isinstance(reported, list) or not reported:
        return None
    artifacts = []
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
    return artifacts


def _task_object(
    record: TaskRecord, context_id: str, history: list[A2AMessage] | None = None
) -> Task:
    """Render a stored task as the A2A Task a worker or debugger receives."""

    status_message = _agent_message(
        record.instructions,
        context_id=context_id,
        task_id=record.id,
        metadata={"kind": "assignment", "role": record.role, "title": record.title},
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
            "role": record.role,
            "title": record.title,
            "assignee": record.assignee,
            "lease_expires": record.lease_expires,
            "result": record.result,
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
        kind = metadata.get("kind", "progress")
        if kind == "result":
            return self._result(task, agent, _text(message), metadata)
        if kind == "progress":
            self.store.record_progress(task.id, agent.name, _text(message))
            return _agent_message(
                "noted",
                context_id=agent.context_id,
                task_id=task.id,
                metadata={"kind": "progress_ack"},
            )
        raise _invalid(f"metadata.kind {kind!r} is not a message/send intent on a task")

    def _check_in(self, message: A2AMessage, metadata: dict[str, Any]) -> A2AMessage:
        if _text(message).upper() != CHECK_IN_TEXT:
            raise _invalid(
                f"a message with no taskId must be the {CHECK_IN_TEXT} check-in"
            )
        name = metadata.get("agent")
        if not isinstance(name, str) or not name.strip():
            raise _invalid("check-in requires metadata.agent")
        runtime = metadata.get("runtime")
        agent = self.store.check_in(
            name.strip(),
            _string_list(metadata.get("capabilities"), "capabilities"),
            runtime=str(runtime) if isinstance(runtime, str) else None,
        )
        return _agent_message(
            "REGISTERED",
            context_id=agent.context_id,
            metadata={
                "kind": "check_in_ack",
                "agent": agent.name,
                "status": agent.status.value,
                "contextId": agent.context_id,
            },
        )

    def _result(
        self,
        task: TaskRecord,
        agent: AgentRecord,
        summary: str,
        metadata: dict[str, Any],
    ) -> Task:
        raw_status = metadata.get("status")
        try:
            status = TaskState(str(raw_status))
        except ValueError as exc:
            raise _invalid("metadata.status must be 'completed' or 'failed'") from exc
        artifacts = metadata.get("artifacts") or []
        if not isinstance(artifacts, list):
            raise _invalid("metadata.artifacts must be a list")
        finished = self.store.submit_result(
            task.id,
            agent.name,
            status,
            summary,
            [item if isinstance(item, dict) else {"value": item} for item in artifacts],
        )
        return _task_object(finished, agent.context_id)

    # -- message/stream -----------------------------------------------------

    def _stream(self, request_id: RequestId, params: MessageSendParams) -> Response:
        message = params.message
        metadata = _metadata(message)
        timeout_s = self._timeout(metadata)

        if message.task_id is not None:
            if metadata.get("kind") != "question":
                raise _invalid("a streaming call on a task must be metadata.kind=question")
            agent = self._resolve_agent(message, metadata)
            task = self._owned_task(message.task_id, agent)
            question = _text(message)
            if not question:
                raise _invalid("a question needs text")
            # The task is parked and Alice is notified before the response body
            # opens, so a client that disconnects still leaves the question with
            # her rather than losing it with the stream.
            question_id = self.store.open_question(task.id, agent.name, question)
            return self._streaming(
                self._reply_stream(request_id, task, agent, question_id, timeout_s)
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
                        metadata={"kind": "release", "release": True},
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
    ) -> AsyncIterator[bytes]:
        reply = await self.store.await_reply(task.id, question_id, timeout_s)
        if reply is None:
            yield _sse(
                _success_body(request_id, self._timeout_message(agent.context_id, task.id))
            )
            return
        yield _sse(_success_body(request_id, _stored_message(reply)))

    def _timeout_message(self, context_id: str, task_id: str | None = None) -> A2AMessage:
        """Tell the worker the hold elapsed; the guide says to call again."""

        return _agent_message(
            "TIMEOUT",
            context_id=context_id,
            task_id=task_id,
            metadata={"kind": "timeout", "timeout": True},
        )

    def _timeout(self, metadata: Mapping[str, Any]) -> float:
        requested = metadata.get("timeout_s")
        if requested is None:
            return self.settings.bounded_wait(None)
        if not isinstance(requested, int | float) or isinstance(requested, bool):
            raise _invalid("metadata.timeout_s must be a number of seconds")
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
        name = metadata.get("agent")
        if isinstance(name, str) and name.strip():
            agent = self.store.agent_by_name(name.strip())
            if agent is None:
                raise _invalid(f"unknown agent {name!r}; check in first")
            return agent
        raise _invalid("the message needs a contextId or metadata.agent")

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
