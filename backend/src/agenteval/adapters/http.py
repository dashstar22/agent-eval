"""通用 HTTP 契约：POST /，只发送 question/execution_id/preconditions。"""

import time
from typing import Mapping
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ..config import ConnectionConfig
from ..models import Context, ExecutionResult, ObservedVersion, ToolEvent, Usage, now
from ..safety import redact
from .base import Budget, HttpSession, ProtocolFailure, SaveAnswer, request_json, result_error


class HttpAdapter:
    def __init__(self, config: ConnectionConfig, *, transport: httpx.BaseTransport | None = None, environment: Mapping[str, str] | None = None, before_request=None):
        self.config, self.transport, self.environment = config, transport, environment
        self.before_request = before_request

    def run(self, question: str, save_answer: SaveAnswer | None = None, *, preconditions: list[str] | None = None, expected_answer=None, expected_tools=None) -> ExecutionResult:
        started, stamp, execution_id = time.monotonic(), now(), str(uuid4())
        try:
            bearer = self.config.bearer(self.environment)
            headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
            with HttpSession(transport=self.transport, before_request=self.before_request) as client:
                raw, _, status = request_json(client, Budget(self.config, started), "POST", self.config.base_url,
                    headers=headers, json={"execution_id": execution_id, "question": question, "preconditions": preconditions or []})
            if status < 200 or status >= 300:
                raise ProtocolFailure(f"HTTP_{status}")
            if not isinstance(raw.get("answer"), str):
                raise ProtocolFailure("MISSING_OR_INVALID_ANSWER")
            raw = redact(raw, (bearer or "",))
            tools = raw.get("tools")
            usage = Usage.model_validate(raw.get("usage", {}))
            result = ExecutionResult(
                execution_id=execution_id, answer=raw["answer"], business_status=raw.get("business_status"),
                agent_status="SUCCEEDED", started_at=stamp, ended_at=now(), duration_ms=round((time.monotonic() - started) * 1000),
                source_type=self.config.source_type, trace_ref=raw.get("trace_ref"),
                tools=None if tools is None else [ToolEvent.model_validate(item) for item in tools],
                tools_completeness=raw.get("tools_completeness", "UNAVAILABLE" if tools is None else "PARTIAL"),
                contexts=None if raw.get("contexts") is None else [Context.model_validate(item) for item in raw["contexts"]],
                context_scope=raw.get("context_scope", "UNAVAILABLE"), context_completeness=raw.get("context_completeness", "UNAVAILABLE"),
                usage=usage, observed_version=ObservedVersion.model_validate(raw.get("observed_version", {})),
                raw_agent={"source_type": self.config.source_type, "response": redact(raw, (bearer or "",))},
                observation_reason="INLINE_HTTP_CONTRACT", observation_status="PARTIAL" if tools is not None else "UNAVAILABLE",
            )
        except httpx.TimeoutException:
            result = result_error(self.config, stamp, started, "AGENT_TIMEOUT_RESULT_UNKNOWN", "TIMEOUT", execution_id=execution_id)
        except httpx.HTTPError:
            result = result_error(self.config, stamp, started, "TRANSPORT_ERROR_RESULT_UNKNOWN", "RESULT_UNKNOWN", execution_id=execution_id)
        except (ProtocolFailure, ValidationError, ValueError, TypeError) as failure:
            code = failure.code if isinstance(failure, ProtocolFailure) else "INVALID_HTTP_CONTRACT"
            result = result_error(self.config, stamp, started, code, "PROTOCOL_ERROR", execution_id=execution_id)
        if save_answer:
            save_answer(result)
        return result
