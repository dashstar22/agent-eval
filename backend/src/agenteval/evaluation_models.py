"""规则、Judge 和持久化共享的评分契约；执行事实沿用 ExecutionResult。"""

from typing import Any, Literal

from pydantic import Field, StrictInt, field_validator

from .models import ExecutionResult, Record, Usage

MetricStatus = Literal["PASS", "FAIL", "NA", "ERROR"]
Conclusion = Literal["PASS", "FAIL", "UNDETERMINED"]


class MetricResult(Record):
    metric: str
    status: MetricStatus
    score: float | int | None = None
    required: bool = False
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class JudgedScore(Record):
    score: StrictInt = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=20000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("评分理由不能为空")
        return value.strip()


class JudgeAttemptRecord(Record):
    attempt: int = Field(ge=1, le=2)
    status: Literal["SUCCEEDED", "ERROR"]
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] | None = None
    usage: Usage = Field(default_factory=Usage)
    duration_ms: int = Field(default=0, ge=0)
    error: dict[str, Any] | None = None


class JudgeResult(Record):
    status: Literal["SUCCEEDED", "ERROR", "NA"]
    scores: dict[str, JudgedScore] = Field(default_factory=dict)
    attempts: list[JudgeAttemptRecord] = Field(default_factory=list)
    error_code: str | None = None


class CaseAssessment(Record):
    metrics: list[MetricResult]
    conclusion: Conclusion
    judge_status: Literal["SUCCEEDED", "ERROR", "NA"] = "NA"


class SummaryCase(Record):
    case_key: str
    execution: ExecutionResult | None = None
    assessment: CaseAssessment | None = None
    judge: JudgeResult | None = None
