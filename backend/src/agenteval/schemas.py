"""管理接口及冻结输入；参考行为只属于评测侧。"""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .config import ConnectionConfig

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
Reference = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]
METRICS = {"execution_completed", "answer_nonempty", "correctness", "relevance", "task_completion",
           "faithfulness", "expected_tools", "tool_success", "response_time", "tool_names", "tool_order", "tool_call_count"}
DEFAULT_REQUIRED = ["execution_completed", "answer_nonempty", "correctness", "relevance", "task_completion"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolAssertions(Input):
    sequence: list[Text] | None = Field(default=None, min_length=1, max_length=30)
    max_calls: dict[Text, Annotated[int, Field(ge=0, le=1000)]] = Field(default_factory=dict, max_length=100)

    @field_validator("sequence")
    @classmethod
    def unique_sequence(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("工具顺序不能包含重复名称；比较各工具首次出现顺序")
        return value


class DatasetCreate(Input):
    name: Text
    description: str = Field(default="", max_length=10000)


class DatasetPatch(Input):
    name: Text | None = None
    description: str | None = Field(default=None, max_length=10000)


class Preconditions(Input):
    device_ref: str | None = Field(default=None, max_length=200)
    order_id: int | None = Field(default=None, gt=0)
    prior_turns: list[Question] = Field(default_factory=list, max_length=19)


class CaseCreate(Input):
    case_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    question: Question
    expected_answer: Question
    expected_tools: list[Text] | None = Field(default=None, max_length=100)
    tool_match_mode: Literal["contains", "exact"] = "contains"
    tool_assertions: ToolAssertions = Field(default_factory=ToolAssertions)
    required_metrics: list[str] = Field(default_factory=lambda: DEFAULT_REQUIRED.copy(), max_length=20)
    tags: list[Text] = Field(default_factory=list, max_length=50)
    enabled: bool = True
    preconditions: Preconditions = Field(default_factory=Preconditions)
    source: dict[str, Any] = Field(default_factory=dict)
    license: str = Field(default="UNSPECIFIED", min_length=1, max_length=100)
    seen_regression: bool = False

    @model_validator(mode="after")
    def matching(self):
        if self.expected_tools == [] and self.tool_match_mode != "exact":
            raise ValueError("expected_tools 为空列表时须使用 exact；不检查请用 null")
        if not self.required_metrics or not set(self.required_metrics) <= METRICS:
            raise ValueError("required_metrics 须包含至少一个已知指标")
        if "tool_order" in self.required_metrics and not self.tool_assertions.sequence:
            raise ValueError("必评工具顺序须配置 sequence")
        if "tool_call_count" in self.required_metrics and not self.tool_assertions.max_calls:
            raise ValueError("必评工具次数须配置 max_calls")
        for values in (self.expected_tools or [], self.tags, self.required_metrics):
            if len(set(values)) != len(values):
                raise ValueError("工具、标签及指标不能重复")
        return self


class CasePatch(Input):
    question: Question | None = None
    expected_answer: Question | None = None
    expected_tools: list[Text] | None = Field(default=None, max_length=100)
    tool_match_mode: Literal["contains", "exact"] | None = None
    tool_assertions: ToolAssertions | None = None
    required_metrics: list[str] | None = None
    tags: list[Text] | None = None
    enabled: bool | None = None
    preconditions: Preconditions | None = None
    source: dict[str, Any] | None = None
    license: str | None = Field(default=None, min_length=1, max_length=100)
    seen_regression: bool | None = None


class CredentialRefs(Input):
    username_ref: Reference | None = None
    password_ref: Reference | None = None
    bearer_ref: Reference | None = None


class AgentCreate(Input):
    name: Text
    adapter_type: Literal["MOCK", "HTTP", "PRINTER_HTTP"]
    base_url: str = Field(default="http://127.0.0.1", max_length=2048)
    timeout_ms: int = Field(default=195000, ge=1, le=360000)
    credential_refs: CredentialRefs = Field(default_factory=CredentialRefs)
    expected_version: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    source_type: Literal["MOCK", "PROTOCOL_DOUBLE", "REAL_EXTERNAL"] = "MOCK"

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value):
        return ConnectionConfig.safe_url(value)

    @field_validator("expected_version")
    @classmethod
    def safe_versions(cls, value):
        if not set(value) <= {"build_sha256", "prompt_sha256", "model_id", "knowledge_version", "version_label"}:
            raise ValueError("expected_version 含未知字段")
        if any(len(v) > 500 for v in value.values()):
            raise ValueError("版本字段过长")
        return value

    @model_validator(mode="after")
    def connection(self):
        if self.adapter_type == "MOCK" and self.source_type != "MOCK":
            raise ValueError("Mock 连接必须保留 MOCK 来源")
        if self.adapter_type != "MOCK" and self.source_type == "MOCK":
            raise ValueError("HTTP 连接须声明 PROTOCOL_DOUBLE 或 REAL_EXTERNAL")
        refs = self.credential_refs
        if self.adapter_type == "PRINTER_HTTP" and (not refs.username_ref or not refs.password_ref):
            raise ValueError("PrinterHttp 须配置 username_ref 与 password_ref")
        return self


class AgentPatch(Input):
    name: Text | None = None
    adapter_type: Literal["MOCK", "HTTP", "PRINTER_HTTP"] | None = None
    base_url: str | None = None
    timeout_ms: int | None = None
    credential_refs: CredentialRefs | None = None
    expected_version: dict[str, str] | None = None
    enabled: bool | None = None
    source_type: Literal["MOCK", "PROTOCOL_DOUBLE", "REAL_EXTERNAL"] | None = None


class JudgeProfile(Input):
    base_url: str | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    credential_ref: Reference | None = None
    source_type: Literal["REAL_EXTERNAL", "PROTOCOL_DOUBLE"] = "REAL_EXTERNAL"
    rubric_version: str = Field(default="agenteval-rubric-v1", min_length=1, max_length=100)
    rubric: dict[str, str] = Field(default_factory=lambda: {
        "1": "严重错误", "2": "主要目标未满足", "3": "部分可用但缺关键点",
        "4": "满足目标且有小缺漏", "5": "完整满足参考行为"})
    timeout_seconds: int = Field(default=60, ge=1, le=360)

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value):
        return None if value is None else ConnectionConfig.safe_url(value)

    @field_validator("rubric")
    @classmethod
    def valid_rubric(cls, value):
        if set(value) != {"1", "2", "3", "4", "5"} or any(not v.strip() or len(v) > 2000 for v in value.values()):
            raise ValueError("量表须包含 1 至 5 的非空说明")
        return value


class RuleProfile(Input):
    version: str = Field(default="agenteval-rules-v1", min_length=1, max_length=100)
    quality_thresholds: dict[str, int] = Field(default_factory=lambda: {"correctness": 4, "relevance": 4, "task_completion": 4})
    response_time_ms: int = Field(default=195000, ge=1, le=3600000)
    tool_sources: list[Literal["CHAT_TOOL", "SERVER_COORDINATOR"]] = Field(default_factory=lambda: ["CHAT_TOOL"])

    @field_validator("quality_thresholds")
    @classmethod
    def thresholds(cls, value):
        if not {"correctness", "relevance", "task_completion"} <= set(value) or not set(value) <= {"correctness", "relevance", "task_completion", "faithfulness"}:
            raise ValueError("质量阈值须包含正确性、相关性、任务完成度")
        if any(type(v) is not int or not 1 <= v <= 5 for v in value.values()):
            raise ValueError("质量阈值须为 1 至 5 整数")
        return value

    @field_validator("tool_sources")
    @classmethod
    def sources(cls, value):
        if not value or len(value) != len(set(value)):
            raise ValueError("工具来源须为非空且无重复的集合")
        return value


class ExecutionBudget(Input):
    case_timeout_seconds: int = Field(default=360, ge=1, le=3600)
    observation_wait_seconds: int = Field(default=10, ge=0, le=60)
    response_max_bytes: int = Field(default=2097152, ge=256, le=16777216)
    max_agent_calls: int = Field(default=1, ge=1, le=20)
    total_timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    max_total_calls: int | None = Field(default=None, ge=1, le=10000)


class FreezeRequest(Input):
    dataset_id: int = Field(gt=0)
    agent_config_id: int = Field(gt=0)
    version_label: str = Field(default="", max_length=200)
    judge_profile: JudgeProfile = Field(default_factory=JudgeProfile)
    rule_profile: RuleProfile = Field(default_factory=RuleProfile)
    execution_budget: ExecutionBudget = Field(default_factory=ExecutionBudget)


def canonical_id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("须为规范 UUID")
    return value
