"""启动任务的公开输入；与只冻结输入共用数据及预算模型。"""

from pydantic import Field, field_validator

from .schemas import FreezeRequest, canonical_id


class StartRunRequest(FreezeRequest):
    request_id: str = Field(min_length=36, max_length=36)

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value):
        return canonical_id(value)
