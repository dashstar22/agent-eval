"""保存结果分析的严格输入；不包含执行或模型连接选项。"""

from pydantic import Field, field_validator, model_validator

from .schemas import Input, canonical_id


class AnalysisSelection(Input):
    run_id: str
    score_set_id: str | None = None

    @field_validator("run_id", "score_set_id")
    @classmethod
    def valid_identity(cls, value):
        return canonical_id(value) if value is not None else None


class RegressionPolicy(Input):
    max_new_failures: int = Field(default=0, ge=0, le=100000)
    min_coverage: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
    max_pass_rate_drop: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)


class RegressionCheckInput(Input):
    baseline: AnalysisSelection
    candidate: AnalysisSelection
    policy: RegressionPolicy = Field(default_factory=RegressionPolicy)


class StabilityAnalysisInput(Input):
    selections: list[AnalysisSelection] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def distinct_executions(self):
        ids = [item.run_id for item in self.selections]
        if len(set(ids)) != len(ids):
            raise ValueError("稳定性分析须选择不同运行；同一执行的重评分不是重复执行")
        return self
