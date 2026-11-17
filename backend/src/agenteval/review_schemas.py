"""人工确认严格输入；填报姓名只是本机用户声明。"""

from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints, field_validator

from .schemas import Input, Question, Text, canonical_id

QUALITY_METRICS = ("correctness", "relevance", "task_completion", "faithfulness")
Score = Annotated[StrictInt, Field(ge=1, le=5)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]


class HumanReviewCreate(Input):
    case_run_id: str
    score_set_id: str
    reviewer_name: Text
    conclusion: Literal["PASS", "FAIL", "UNDETERMINED"]
    scores: dict[str, Score] = Field(default_factory=dict, max_length=4)
    reason: Reason
    human_confirmed: bool

    @field_validator("case_run_id", "score_set_id")
    @classmethod
    def valid_ids(cls, value):
        return canonical_id(value)

    @field_validator("human_confirmed")
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError("须由用户明确确认人工填报")
        return value

    @field_validator("scores")
    @classmethod
    def known_metrics(cls, value):
        if not set(value) <= set(QUALITY_METRICS):
            raise ValueError("只能填写已知质量指标")
        return value


class RegressionDraftCreate(Input):
    case_run_id: str
    score_set_id: str
    target_dataset_id: int = Field(gt=0)
    case_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    expected_answer: Question
    reason: Reason

    @field_validator("case_run_id", "score_set_id")
    @classmethod
    def valid_ids(cls, value):
        return canonical_id(value)


class RegressionDraftApproval(Input):
    reviewer_name: Text
    human_confirmed: bool

    @field_validator("human_confirmed")
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError("须由用户明确确认审核")
        return value
