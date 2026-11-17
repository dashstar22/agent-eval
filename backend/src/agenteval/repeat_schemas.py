"""明确次数、调用许可及幂等请求身份。"""

from pydantic import Field, field_validator

from .schemas import Input, canonical_id


class RepeatRequest(Input):
    request_id: str
    source_run_id: str
    repeat_count: int = Field(ge=2, le=5)
    confirm_real_execution: bool = False

    @field_validator("request_id", "source_run_id")
    @classmethod
    def valid_id(cls, value):
        return canonical_id(value)
