"""Protocol values shared by persistence and later A2A handlers."""

from enum import StrEnum


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
