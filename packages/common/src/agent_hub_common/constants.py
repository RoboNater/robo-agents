"""Constants and metadata keys used across hub and worker packages."""

from enum import StrEnum


class MetaKeys(StrEnum):
    """Hub-specific metadata keys namespaced with `hub.` (spec §4.0)."""

    KIND = "hub.kind"
    AGENT = "hub.agent"
    CAPABILITIES = "hub.capabilities"
    RUNTIME = "hub.runtime"
    STATUS = "hub.status"
    TIMEOUT = "hub.timeout"
    TIMEOUT_S = "hub.timeout_s"
    RETRY_AS_MESSAGE_ID = "hub.retry_as_message_id"
    RELEASE = "hub.release"
    RESULT = "hub.result"
    ROLE = "hub.role"
    TITLE = "hub.title"
    ASSIGNEE = "hub.assignee"
    LEASE_EXPIRES = "hub.lease_expires"
    ARTIFACTS = "hub.artifacts"
    STATE = "hub.state"
    SENDER = "hub.sender"
    TS = "hub.ts"
    SCHEMA_VERSION = "hub.schema_version"
    OPERATION_ID = "hub.operation_id"
