"""配置只保存环境变量引用，不序列化认证信息。"""

import os
import re
from typing import Literal, Mapping
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from .models import Record, SourceType


class ConnectionConfig(Record):
    base_url: str
    source_type: SourceType = "REAL_EXTERNAL"
    request_timeout_seconds: float = Field(default=195.0, gt=0, le=360)
    case_timeout_seconds: float = Field(default=360.0, gt=0, le=3600)
    observation_wait_seconds: float = Field(default=10.0, ge=0, le=60)
    observation_interval_seconds: float = Field(default=0.5, ge=0.001, le=10)
    response_max_bytes: int = Field(default=2 * 1024 * 1024, ge=256, le=16 * 1024 * 1024)
    max_agent_calls: int = Field(default=1, ge=1, le=20)
    username_ref: str = "PRINTER_USERNAME"
    password_ref: str = "PRINTER_PASSWORD"
    bearer_ref: str | None = None

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value):
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("base_url requires HTTP(S), no embedded credentials/query/fragment")
        return value.rstrip("/")

    @field_validator("username_ref", "password_ref", "bearer_ref")
    @classmethod
    def valid_reference(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
            raise ValueError("credential reference must be an environment variable name")
        return value

    def credentials(self, environment: Mapping[str, str] | None = None) -> tuple[str, str]:
        env = os.environ if environment is None else environment
        user, password = env.get(self.username_ref), env.get(self.password_ref)
        if not user or not password:
            raise ValueError("CREDENTIAL_REFERENCE_UNRESOLVED")
        return user, password

    def bearer(self, environment: Mapping[str, str] | None = None) -> str | None:
        if self.bearer_ref is None:
            return None
        env = os.environ if environment is None else environment
        if not env.get(self.bearer_ref):
            raise ValueError("CREDENTIAL_REFERENCE_UNRESOLVED")
        return env[self.bearer_ref]
