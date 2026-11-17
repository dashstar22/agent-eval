"""三种适配器共享的、保留未知状态的执行事实契约。"""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

Availability = Literal["COMPLETE", "PARTIAL", "UNAVAILABLE", "NOT_REQUESTED"]
SourceType = Literal["MOCK", "PROTOCOL_DOUBLE", "REAL_EXTERNAL"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UsageAttempt(Record):
    attempt: int = Field(ge=1)
    model_id: str | None = None
    source: str | None = None
    requested_model_id: str | None = None
    prompt_sha256: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    availability: Availability = "UNAVAILABLE"
    status: str = "UNKNOWN"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class Usage(Record):
    status: Availability = "UNAVAILABLE"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    attempts: list[UsageAttempt] = Field(default_factory=list)
    reason: str | None = "NOT_PROVIDED"
    cost: float | None = None

    @model_validator(mode="after")
    def consistent(self):
        counts = (self.input_tokens, self.output_tokens, self.total_tokens)
        if self.status == "COMPLETE" and any(v is None for v in counts):
            raise ValueError("COMPLETE usage requires all three real counters")
        if self.status in {"UNAVAILABLE", "NOT_REQUESTED"} and any(v is not None for v in counts):
            raise ValueError("unavailable usage cannot contain counters")
        return self


class ToolEvent(Record):
    event_id: str
    sequence: int = Field(ge=1)
    source: Literal["CHAT_TOOL", "SERVER_COORDINATOR", "UNKNOWN"]
    kind: str = "TOOL"
    name: str
    status: str
    duration_ms: int | None = Field(default=None, ge=0)
    result: Any = None
    completeness: Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"] = "COMPLETE"


class Context(Record):
    text: str
    source: str | None = None
    version: str | int | None = None
    point_id: str | None = None
    content: str | None = None
    title: str | None = None
    simulated: bool | None = None
    use_condition: str | None = None
    models: list[str] | None = None
    model_scope: str | None = None
    page_number: str | int | None = None
    section: str | None = None
    # 完整保留来源字段和未来扩展；规范字段不静默丢弃适用条件。
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservedVersion(Record):
    build_sha256: str | None = None
    prompt_sha256: str | None = None
    prompt_sha256s: list[str] = Field(default_factory=list)
    model_id: str | None = None
    knowledge_versions: list[Any] = Field(default_factory=list)
    verification: str = "UNKNOWN"
    build_status: str = "UNKNOWN"
    model_status: str = "UNKNOWN"


class ExecutionResult(Record):
    execution_id: str = Field(default_factory=lambda: str(uuid4()))
    answer: str | None = None
    business_status: str | None = None
    agent_status: Literal["SUCCEEDED", "FAILED", "TIMEOUT", "RESULT_UNKNOWN", "PROTOCOL_ERROR"]
    started_at: str
    ended_at: str
    duration_ms: int = Field(ge=0)
    source_type: SourceType
    model_execution_verified: bool = False
    trace_ref: str | None = None
    conversation_id: str | None = None
    turn_id: str | None = None
    order_id: int | None = None
    replayed: bool = False
    original_trace_id: str | None = None
    tools: list[ToolEvent] | None = None
    tools_completeness: Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"] = "UNAVAILABLE"
    contexts: list[Context] | None = None
    context_scope: Literal["ACTUAL_MODEL_CONTEXT", "CITATION_SUBSET", "NOT_USED", "UNAVAILABLE"] = "UNAVAILABLE"
    context_completeness: Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"] = "UNAVAILABLE"
    usage: Usage = Field(default_factory=Usage)
    observed_version: ObservedVersion = Field(default_factory=ObservedVersion)
    observation_status: Literal["PENDING", "COMPLETE", "PARTIAL", "UNAVAILABLE"] = "UNAVAILABLE"
    observation_reason: str | None = "NOT_PROVIDED"
    raw_agent: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    prerequisites: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self):
        if self.agent_status == "SUCCEEDED" and self.answer is None:
            raise ValueError("successful execution requires an answer field")
        if self.tools_completeness == "COMPLETE" and self.tools is None:
            raise ValueError("complete tools require a list; [] means proven empty")
        if self.tools is not None:
            seq = [e.sequence for e in self.tools]
            ids = [e.event_id for e in self.tools]
            if seq != sorted(set(seq)) or len(ids) != len(set(ids)):
                raise ValueError("events must be ordered and unique")
        if self.source_type != "REAL_EXTERNAL" and self.model_execution_verified:
            raise ValueError("simulation is not real model evidence")
        return self


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_uuid(value: str) -> str:
    parsed = str(UUID(value))
    if parsed != value:
        raise ValueError("expected canonical UUID")
    return parsed
