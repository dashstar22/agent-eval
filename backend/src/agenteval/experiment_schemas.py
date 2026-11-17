"""实验维护与重新评分输入；只接受非敏感配置和凭证引用。"""

from pydantic import Field, field_validator

from .schemas import ExecutionBudget, Input, JudgeProfile, RuleProfile, Text, canonical_id


class ExperimentCreate(Input):
    name: Text
    dataset_id: int = Field(gt=0)
    agent_config_id: int = Field(gt=0)
    version_label: str = Field(default="", max_length=200)
    purpose: str = Field(default="", max_length=10000)


class ExperimentPatch(Input):
    name: Text | None = None
    dataset_id: int | None = Field(default=None, gt=0)
    agent_config_id: int | None = Field(default=None, gt=0)
    version_label: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=10000)


class ExperimentStart(Input):
    request_id: str = Field(min_length=36, max_length=36)
    judge_profile: JudgeProfile = Field(default_factory=JudgeProfile)
    rule_profile: RuleProfile = Field(default_factory=RuleProfile)
    execution_budget: ExecutionBudget = Field(default_factory=ExecutionBudget)

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value):
        return canonical_id(value)


class RescoreRequest(Input):
    source_run_id: str
    parent_score_set_id: str | None = None
    judge_profile: JudgeProfile | None = None
    rule_profile: RuleProfile | None = None

    @field_validator("source_run_id", "parent_score_set_id")
    @classmethod
    def valid_uuid(cls, value):
        return None if value is None else canonical_id(value)
