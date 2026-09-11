"""Protocol values shared by persistence and later A2A handlers."""

from dataclasses import dataclass, field
from enum import StrEnum

# A profile field the adapter was not told is recorded as this, never guessed
# (spec §4.3), so Alice can tell "not reported" apart from a real value.
UNKNOWN = "unknown"


class WorkflowStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    ESCALATED = "escalated"


class AgentStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    RELEASED = "released"
    LOST = "lost"


class TaskState(StrEnum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class EventKind(StrEnum):
    AGENT_CHECKED_IN = "agent_checked_in"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    WORKER_QUESTION = "worker_question"
    LEASE_EXPIRED = "lease_expired"
    AGENT_LOST = "agent_lost"


class ModelSource(StrEnum):
    """Where a worker's `model` came from (spec §3).

    `env` is the operator's launcher configuration (`HUB_MODEL`); `declared` is
    the agent naming its own model at check-in, used only when the launcher
    names none. Neither is attested (§1 non-goals).
    """

    DECLARED = "declared"
    ENV = "env"
    UNKNOWN = UNKNOWN


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """A worker's self-reported identity, which role selection evaluates (§5).

    Observational only: the hub records what the worker says and verifies none
    of it. `workspace_id` stays None until the worker reports one (#28).
    """

    harness: str = UNKNOWN
    harness_version: str = UNKNOWN
    provider: str = UNKNOWN
    model: str = UNKNOWN
    model_source: ModelSource = ModelSource.UNKNOWN
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    workspace_id: str | None = None
