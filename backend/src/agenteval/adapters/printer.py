"""真实售后 Session/CSRF 接入；回答先交付，观测只做有界 GET。"""

import time
from typing import Any, Mapping
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ..config import ConnectionConfig
from ..models import Context, ExecutionResult, ObservedVersion, ToolEvent, Usage, UsageAttempt, canonical_uuid, now
from ..safety import redact
from .base import Budget, HttpSession, ProtocolFailure, SaveAnswer, request_json, result_error


class PersistenceFailure(RuntimeError):
    """保存失败必须停止后续网络调用，不转成可继续的协议错误。"""


TECHNICAL_FAILURES = {"MODEL_UNAVAILABLE", "TURN_TIMEOUT", "MODEL_TIMEOUT", "ASSISTANT_BUSY", "DATABASE_UNAVAILABLE", "WRITE_RESULT_UNKNOWN", "ASSISTANT_NOT_CONNECTED", "RETRIEVAL_UNAVAILABLE", "MODEL_RESPONSE_REJECTED", "TOOL_TIMEOUT", "ASSISTANT_CANCELLED", "CHECKPOINT_UNAVAILABLE", "MODEL_CALL_LIMIT", "TOOL_CALL_LIMIT", "AI_DISABLED", "RESULT_UNKNOWN"}


class PrinterHttpAdapter:
    def __init__(self, config: ConnectionConfig, *, transport: httpx.BaseTransport | None = None, environment: Mapping[str, str] | None = None, before_request=None):
        self.config, self.transport, self.environment = config, transport, environment
        self.before_request = before_request

    def run(self, question: str, save_answer: SaveAnswer, *, order_id: int, preconditions: list[str] | None = None,
            expected_answer=None, expected_tools=None) -> ExecutionResult:
        if not callable(save_answer):
            raise ValueError("save_answer callback is required before observation GET")
        if not isinstance(question, str) or not question.strip() or type(order_id) is not int or order_id < 1:
            raise ValueError("question and positive order_id are required")
        previous = list(preconditions or [])
        if any(not isinstance(text, str) or not text.strip() for text in previous):
            raise ValueError("preconditions must be explicit nonempty user turns")
        if len(previous) + 1 > self.config.max_agent_calls:
            raise ValueError("AGENT_CALL_BUDGET_EXCEEDED")
        started, stamp = time.monotonic(), now()
        budget = Budget(self.config, started)
        conversation_id, turn_id, trace_ref = None, None, None
        prerequisites: list[dict[str, Any]] = []
        with HttpSession(base_url=self.config.base_url, transport=self.transport, before_request=self.before_request) as client:
            try:
                user, password = self.config.credentials(self.environment)
                secrets = (user, password)
                csrf = self._csrf(client, budget)
                _, _, status = request_json(client, budget, "POST", "/api/auth/login", data={"username": user, "password": password}, headers=csrf)
                self._require(status, "LOGIN_REJECTED")
                csrf = self._csrf(client, budget)
                order, _, status = request_json(client, budget, "GET", f"/api/orders/{order_id}")
                self._require(status, "ORDER_ACCESS_REJECTED")
                if order.get("id") != order_id:
                    raise ProtocolFailure("ORDER_ID_MISMATCH")
                detail, _, status = request_json(client, budget, "POST", "/api/conversations", json={"orderId": order_id}, headers=csrf)
                self._require(status, "CONVERSATION_REJECTED")
                conversation_id = canonical_uuid(detail["id"])
                for index, content in enumerate([*previous, question]):
                    turn_id = str(uuid4())
                    body, headers, status = request_json(client, budget, "POST", f"/api/conversations/{conversation_id}/messages",
                        json={"turnId": turn_id, "content": content}, headers=csrf)
                    self._require(status, "TURN_REJECTED")
                    if body.get("conversationId") != conversation_id or body.get("turnId") != turn_id:
                        raise ProtocolFailure("TURN_CORRELATION_MISMATCH")
                    trace_ref = canonical_uuid(headers["x-trace-id"])
                    reply = body["assistantMessage"]["response"]
                    if not isinstance(reply.get("text"), str) or not isinstance(reply.get("status"), str) or type(body.get("replayed")) is not bool:
                        raise ProtocolFailure("INVALID_TURN_RESPONSE")
                    result = ExecutionResult(
                        answer=redact(reply["text"], secrets), business_status=reply["status"], agent_status="FAILED" if reply["status"] in TECHNICAL_FAILURES else "SUCCEEDED",
                        started_at=stamp, ended_at=now(), duration_ms=round((time.monotonic() - started) * 1000),
                        source_type=self.config.source_type, trace_ref=trace_ref, conversation_id=conversation_id,
                        turn_id=turn_id, order_id=order_id, replayed=body["replayed"],
                        raw_agent={"source_type": self.config.source_type, "response": redact(body, secrets)},
                        observation_status="PENDING", observation_reason="ANSWER_SAVED_BEFORE_OBSERVATION",
                        prerequisites=prerequisites.copy(),
                    )
                    # Persistence failure deliberately propagates: never issue another turn/GET after it.
                    self._save(save_answer, result)
                    enriched = self.collect_observations(client, budget, result, secrets=secrets)
                    if index < len(previous):
                        prerequisites.append(enriched.model_dump())
                        if reply["status"] in TECHNICAL_FAILURES:
                            return self._update(enriched, agent_status="FAILED", error_code="PRECONDITION_FAILED")
                    else:
                        return enriched
            except httpx.TimeoutException:
                result = result_error(self.config, stamp, started, "AGENT_TIMEOUT_RESULT_UNKNOWN", "TIMEOUT", conversation_id=conversation_id,
                    turn_id=turn_id, trace_ref=trace_ref, order_id=order_id, prerequisites=prerequisites)
            except httpx.HTTPError:
                result = result_error(self.config, stamp, started, "TRANSPORT_ERROR_RESULT_UNKNOWN", "RESULT_UNKNOWN", conversation_id=conversation_id,
                    turn_id=turn_id, trace_ref=trace_ref, order_id=order_id, prerequisites=prerequisites)
            except (ProtocolFailure, ValidationError, ValueError, KeyError, TypeError) as failure:
                code = failure.code if isinstance(failure, ProtocolFailure) else "INVALID_PRINTER_CONTRACT"
                result = result_error(self.config, stamp, started, code, "PROTOCOL_ERROR", conversation_id=conversation_id,
                    turn_id=turn_id, trace_ref=trace_ref, order_id=order_id, prerequisites=prerequisites)
            self._save(save_answer, result)
            return result

    def collect_observations(self, client: HttpSession, budget: Budget, result: ExecutionResult, *, secrets: tuple[str, ...] = ()) -> ExecutionResult:
        """可对保存过的回答调用；只读取旧 trace，不请求模型、不重建工具。"""
        if result.trace_ref is None:
            return self._update(result, observation_status="UNAVAILABLE", observation_reason="TRACE_MISSING")
        canonical_uuid(result.trace_ref)
        deadline = min(time.monotonic() + self.config.observation_wait_seconds, budget.started + self.config.case_timeout_seconds)
        reason, partial = "OBSERVATION_DEADLINE", None
        polls = 0
        while True:
            if polls and time.monotonic() >= deadline:
                break
            polls += 1
            try:
                # Observation requests use the remaining observation deadline, never the full Agent timeout.
                remaining = max(0.001, deadline - time.monotonic())
                poll_config = self.config.model_copy(update={"request_timeout_seconds": min(self.config.request_timeout_seconds, remaining)})
                data, _, status = request_json(client, Budget(poll_config, budget.started), "GET", f"/api/evaluation-observations/{result.trace_ref}")
                if status == 200 and data.get("availability") != "UNAVAILABLE":
                    partial = self._import(result, data, secrets)
                    if data.get("complete") is True:
                        return partial
                    reason = data.get("reason") or "OBSERVATION_INCOMPLETE"
                elif status in {404, 410, 503} or data.get("availability") == "UNAVAILABLE":
                    reason = data.get("reason") or data.get("code") or f"HTTP_{status}"
                    if reason in {"DISABLED", "EXPIRED", "CAPACITY", "WRITE_FAILED", "OBSERVATIONS_DISABLED"}:
                        break
                else:
                    reason = f"OBSERVATION_HTTP_{status}"
                    break
            except (ProtocolFailure, ValidationError, ValueError, TypeError, KeyError) as failure:
                reason = failure.code if isinstance(failure, ProtocolFailure) else "INVALID_OBSERVATION_CONTRACT"
                break
            except httpx.HTTPError:
                reason = "OBSERVATION_TRANSPORT_ERROR"
                break
            left = deadline - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(self.config.observation_interval_seconds, left))
        if partial is not None:
            return self._update(partial, observation_status="PARTIAL", observation_reason=str(reason))
        return self._update(result, observation_status="UNAVAILABLE", observation_reason=str(reason))

    def _import(self, result: ExecutionResult, data: dict, secrets: tuple[str, ...]) -> ExecutionResult:
        if any(data.get(key) != value for key, value in (("traceId", result.trace_ref), ("conversationId", result.conversation_id), ("turnId", result.turn_id), ("orderId", result.order_id))):
            raise ProtocolFailure("OBSERVATION_CORRELATION_MISMATCH")
        if type(data.get("complete")) is not bool:
            raise ProtocolFailure("OBSERVATION_COMPLETE_REQUIRED")
        events = data.get("events")
        tools = None if events is None else [ToolEvent(
            event_id=str(event.get("eventId", f"{result.trace_ref}:{event['sequence']}")), sequence=event["sequence"],
            source=event.get("source", "UNKNOWN"), kind=event.get("kind", "TOOL"), name=event.get("name", event.get("tool", "UNKNOWN")),
            status=event.get("status", "UNKNOWN"), duration_ms=event.get("durationMs"), result=redact(event.get("result"), secrets),
            completeness=event.get("completeness", "COMPLETE"),
        ) for event in events if event.get("kind", "TOOL") == "TOOL"]
        contexts = data.get("contexts")
        normalized_contexts, context_changed, context_missing = self._contexts(contexts, secrets)
        context_completeness = data.get("contextCompleteness", "UNAVAILABLE")
        if context_completeness == "COMPLETE" and (context_changed or context_missing):
            context_completeness = "PARTIAL"
        usage_raw = data.get("usage") or {}
        attempts = [UsageAttempt(attempt=a["attempt"], model_id=a.get("modelId"), source=a.get("source"), status=a.get("status", "UNKNOWN"),
            requested_model_id=a.get("requestedModelId"), prompt_sha256=a.get("promptSha256"), duration_ms=a.get("durationMs"), availability=a.get("availability", "UNAVAILABLE"),
            input_tokens=a.get("inputTokens"), output_tokens=a.get("outputTokens"), total_tokens=a.get("totalTokens")) for a in usage_raw.get("attempts", [])]
        replayed = data.get("replayed", result.replayed)
        if type(replayed) is not bool:
            raise ProtocolFailure("INVALID_REPLAY_FLAG")
        usage = Usage(status=usage_raw.get("status", "UNAVAILABLE"), input_tokens=usage_raw.get("inputTokens"),
            output_tokens=usage_raw.get("outputTokens"), total_tokens=usage_raw.get("totalTokens"), attempts=attempts,
            reason=usage_raw.get("reason"), cost=None)
        if replayed:
            usage = Usage(status="NOT_REQUESTED", reason="REPLAY_NO_NEW_MODEL_REQUEST")
        version = data.get("observedVersion") or {}
        complete = data["complete"] and not context_changed and not context_missing
        reason = data.get("reason") or ("CONTEXT_REDACTED_ON_IMPORT" if context_changed else "CONTEXT_FIELDS_MISSING" if context_missing else None)
        source_type, model_verified = self._provenance(result, data, attempts, replayed)
        return self._update(result,
            tools=tools, tools_completeness=data.get("toolsCompleteness", "COMPLETE" if complete and tools is not None else "PARTIAL" if tools is not None else "UNAVAILABLE"),
            contexts=normalized_contexts,
            context_scope=data.get("contextScope", "UNAVAILABLE"), context_completeness=context_completeness,
            usage=usage, replayed=replayed, original_trace_id=data.get("originalTraceId"),
            observed_version=ObservedVersion(build_sha256=version.get("buildSha256"), prompt_sha256=version.get("promptSha256"), prompt_sha256s=version.get("promptSha256s", []),
                model_id=version.get("modelId"), knowledge_versions=version.get("knowledgeVersions", []), verification=version.get("verification", "UNKNOWN"),
                build_status=version.get("buildStatus", "UNKNOWN"), model_status=version.get("modelStatus", "UNKNOWN")),
            observation_status="COMPLETE" if complete else "PARTIAL", observation_reason=reason,
            raw_agent={**result.raw_agent, "source_type": source_type, "observation": redact(data, secrets)},
            # Real source + provider response attempts are evidence; a mode label alone is insufficient.
            source_type=source_type, model_execution_verified=model_verified,
        )

    def _provenance(self, result: ExecutionResult, data: dict, attempts: list[UsageAttempt], replayed: bool):
        source, mode = str(data.get("sourceType", "UNKNOWN")).upper(), str(data.get("modelMode", "UNKNOWN")).upper()
        simulation = any(marker in value for value in (source, mode) for marker in ("SIMULATION", "MOCK", "PROTOCOL_DOUBLE"))
        source_type = result.source_type if self.config.source_type == "REAL_EXTERNAL" else self.config.source_type
        if simulation and source_type == "REAL_EXTERNAL":
            source_type = "PROTOCOL_DOUBLE"
        # 冲突信号保守处理；远程配置仍需成功返回元数据，模式标签本身不是证明。
        verified = source_type == "REAL_EXTERNAL" and self.config.source_type == "REAL_EXTERNAL" and not simulation and not replayed
        verified = verified and source == "HTTP_AGENT" and mode == "REMOTE_API_UNVERIFIED"
        verified = verified and any(a.status == "SUCCEEDED" and a.source == "MODEL_RESPONSE_METADATA" and
            a.model_id is not None and a.model_id.strip().upper() not in {"", "UNKNOWN", "UNAVAILABLE", "NONE", "NULL"} for a in attempts)
        return source_type, bool(verified)

    @staticmethod
    def _contexts(contexts, secrets):
        if contexts is None:
            return None, False, False
        required = {"pointId", "content", "title", "version", "simulated", "useCondition", "models", "modelScope", "pageNumber", "section"}
        normalized, changed, missing = [], False, False
        for original in contexts:
            safe = redact(original, secrets)
            changed = changed or safe != original
            missing = missing or not required.issubset(original)
            normalized.append(Context(
                text=safe.get("text", safe.get("content")), source=safe.get("source", safe.get("title")), version=safe.get("version"),
                point_id=safe.get("pointId"), content=safe.get("content"), title=safe.get("title"), simulated=safe.get("simulated"),
                use_condition=safe.get("useCondition"), models=safe.get("models"), model_scope=safe.get("modelScope"),
                page_number=safe.get("pageNumber"), section=safe.get("section"), metadata=safe,
            ))
        return normalized, changed, missing

    @staticmethod
    def _save(save_answer: SaveAnswer, result: ExecutionResult):
        try:
            save_answer(result)
        except Exception as failure:
            raise PersistenceFailure("ANSWER_PERSISTENCE_FAILED") from failure

    @staticmethod
    def _update(result: ExecutionResult, **changes) -> ExecutionResult:
        return ExecutionResult.model_validate({**result.model_dump(), **changes})

    @staticmethod
    def _require(status: int, code: str):
        if status < 200 or status >= 300:
            raise ProtocolFailure(f"{code}_HTTP_{status}")

    @staticmethod
    def _csrf(client: HttpSession, budget: Budget) -> dict[str, str]:
        data, _, status = request_json(client, budget, "GET", "/api/auth/csrf")
        PrinterHttpAdapter._require(status, "CSRF_REJECTED")
        if not isinstance(data.get("token"), str) or not data["token"] or data.get("headerName") not in {"X-CSRF-TOKEN", "X-XSRF-TOKEN"}:
            raise ProtocolFailure("INVALID_CSRF_RESPONSE")
        return {data["headerName"]: data["token"]}
