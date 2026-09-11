from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator


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


SCHEMA_VERSION: int = 1

SHA_HEX_40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class ImplementerOutcome(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class ReviewerVerdict(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    BLOCKED = "blocked"
    FAILED = "failed"


class TestResult(BaseModel):
    __test__ = False
    command: str
    status: str


class Finding(BaseModel):
    id: str = Field(pattern=r"^r\d+-\d+$")
    text: str


class ImplementerResult(BaseModel):
    outcome: ImplementerOutcome
    pr_url: str | None = None
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40}$")
    commits: list[str] = Field(default_factory=list)
    tests: list[TestResult] = Field(default_factory=list)
    blocker: str | None = None
    resolved_finding_ids: list[str] | None = None
    disputed_finding_ids: list[str] | None = None
    summary: str

    @model_validator(mode="after")
    def validate_completed(self) -> Self:
        if self.outcome == ImplementerOutcome.COMPLETED:
            if not self.pr_url or not self.pr_url.strip():
                raise ValueError("completed implementer result requires pr_url")
            if not self.head_sha or not SHA_HEX_40_RE.match(self.head_sha):
                raise ValueError("completed implementer result requires head_sha")
        return self


class ReviewerResult(BaseModel):
    verdict: ReviewerVerdict
    pr_url: str | None = None
    review_url: str | None = None
    reviewed_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40}$")
    blocking_findings: list[Finding] = Field(default_factory=list)
    nonblocking_findings: list[Finding] = Field(default_factory=list)
    tests: list[TestResult] | None = None
    summary: str

    @model_validator(mode="after")
    def validate_approved(self) -> Self:
        if self.verdict == ReviewerVerdict.APPROVED:
            if not self.reviewed_head_sha or not SHA_HEX_40_RE.match(self.reviewed_head_sha):
                raise ValueError("approved reviewer result requires reviewed_head_sha")
            if len(self.blocking_findings) > 0:
                raise ValueError("approved reviewer result requires blocking_findings to be empty")
        return self


TaskResult = ImplementerResult | ReviewerResult

IMPLEMENTER_RESULT_SCHEMA: dict[str, Any] = ImplementerResult.model_json_schema()
REVIEWER_RESULT_SCHEMA: dict[str, Any] = ReviewerResult.model_json_schema()
