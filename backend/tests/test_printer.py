import json
from uuid import uuid4

import httpx
import pytest

from agenteval.adapters import PrinterHttpAdapter
from agenteval.adapters.printer import PersistenceFailure
from agenteval.config import ConnectionConfig


class PrinterProtocol:
    def __init__(self):
        self.calls, self.turns, self.reads = [], [], 0
        self.conversation = str(uuid4())
        self.trace = str(uuid4())
        self.csrf_calls = 0
        self.behavior = "complete"
        self.saved = []
        self.first_cookie = None

    def __call__(self, req):
        self.calls.append((req.method, req.url.path))
        path = req.url.path
        if path == "/api/auth/csrf":
            if not req.headers.get("cookie"):
                self.csrf_calls = 0
            self.csrf_calls += 1
            return httpx.Response(200, json={"headerName": "X-CSRF-TOKEN", "parameterName": "_csrf", "token": f"csrf-{self.csrf_calls}"}, headers={"Set-Cookie": f"JSESSIONID={'anon' if self.csrf_calls == 1 else 'authenticated'}; Path=/"})
        if path == "/api/auth/login":
            assert req.headers["X-CSRF-TOKEN"] == "csrf-1"
            self.first_cookie = req.headers.get("cookie")
            return httpx.Response(200, json={"id": 1}, headers={"Set-Cookie": "JSESSIONID=authenticated; Path=/"})
        if path == "/api/orders/7":
            assert req.headers.get("cookie") == "JSESSIONID=authenticated"
            if self.behavior == "order_denied":
                return httpx.Response(404, json={"code": "ORDER_NOT_FOUND"})
            return httpx.Response(200, json={"id": 7})
        if path == "/api/conversations":
            self.conversation = str(uuid4())
            assert req.headers["X-CSRF-TOKEN"] == "csrf-2"
            assert json.loads(req.content) == {"orderId": 7}
            return httpx.Response(201, json={"id": self.conversation})
        if path.endswith("/messages"):
            self.trace = str(uuid4())
            body = json.loads(req.content)
            assert set(body) == {"turnId", "content"}
            self.turns.append(body)
            if self.behavior == "turn_timeout":
                raise httpx.ReadTimeout("secret", request=req)
            return httpx.Response(201, json={"conversationId": self.conversation, "turnId": body["turnId"], "replayed": False,
                "assistantMessage": {"response": {"text": "根据资料处理", "status": "MODEL_UNAVAILABLE" if self.behavior == "model_failed" else "ANSWERED", "mode": "REAL"}}}, headers={"X-Trace-Id": self.trace})
        if path.startswith("/api/evaluation-observations/"):
            assert self.saved, "answer must be saved before observation GET"
            self.reads += 1
            if self.behavior in {"missing", "expired", "disabled", "write_failed"} or (self.behavior == "late" and self.reads == 1):
                return httpx.Response(404, json={"availability": "UNAVAILABLE", "reason": {"expired": "EXPIRED", "disabled": "DISABLED", "write_failed": "WRITE_FAILED"}.get(self.behavior, "MISSING")})
            payload = {"traceId": self.trace, "conversationId": self.conversation, "turnId": self.turns[-1]["turnId"], "orderId": 7,
                "complete": self.behavior != "partial", "replayed": self.behavior == "replay", "originalTraceId": None,
                "events": [{"eventId": "e1", "sequence": 1, "source": "SERVER_COORDINATOR", "kind": "TOOL", "name": "getMyOrder", "status": "ORDER_FOUND", "durationMs": 2, "result": {"password": "secret", "id": 7}},
                    {"eventId": "e2", "sequence": 2, "source": "CHAT_TOOL", "kind": "TOOL", "name": "searchKnowledge", "status": "HIT", "durationMs": 3, "result": {"evidence": "真实结果"}}],
                "contexts": [{"text": "资料原文", "content": "资料原文", "source": "demo.txt", "title": "demo.txt", "pointId": "p1", "version": "1", "simulated": True,
                    "useCondition": "断电后操作", "models": ["DEMO"], "modelScope": "EXACT", "pageNumber": 2, "section": "安全"}], "contextScope": "ACTUAL_MODEL_CONTEXT", "contextCompleteness": "COMPLETE",
                "usage": {"status": "PARTIAL", "inputTokens": 10, "outputTokens": None, "totalTokens": None,
                    "attempts": [{"attempt": 1, "modelId": "actual-model", "status": "SUCCEEDED", "source": "MODEL_RESPONSE_METADATA", "inputTokens": 10}]},
                "observedVersion": {"buildSha256": "a" * 64, "promptSha256": "b" * 64, "modelId": "actual-model", "knowledgeVersions": ["1"], "buildStatus": "VERIFIED", "modelStatus": "REQUESTED"}}
            if self.behavior == "mismatch":
                payload["turnId"] = str(uuid4())
            if self.behavior == "out_of_order":
                payload["events"].reverse()
            if self.behavior == "unknown":
                payload["events"] = None
            if self.behavior == "empty":
                payload["events"] = []
            return httpx.Response(200, json=payload)
        raise AssertionError(f"unexpected request {req.method} {path}")


def run(protocol=None, *, observation_wait_seconds=0.006, **kwargs):
    protocol = protocol or PrinterProtocol()
    config = ConnectionConfig(base_url="http://printer.test", source_type="PROTOCOL_DOUBLE", observation_wait_seconds=observation_wait_seconds,
        observation_interval_seconds=0.001, max_agent_calls=3)
    adapter = PrinterHttpAdapter(config, transport=httpx.MockTransport(protocol), environment={"PRINTER_USERNAME": "test-user", "PRINTER_PASSWORD": "private-pass"})
    result = adapter.run("目标问题", protocol.saved.append, order_id=7, **kwargs)
    return result, protocol


def test_login_csrf_refresh_identity_order_turn_gold_and_persistence():
    result, protocol = run(expected_answer="GOLD_NEVER_SEND", expected_tools=["fake"])
    assert protocol.csrf_calls == 2 and protocol.first_cookie == "JSESSIONID=anon"
    assert len(protocol.turns) == 1 and "GOLD" not in json.dumps(protocol.turns)
    assert protocol.saved[0].observation_status == "PENDING" and protocol.saved[0].tools is None
    assert result.observation_status == "COMPLETE" and result.tools[1].result == {"evidence": "真实结果"}
    assert [event.source for event in result.tools] == ["SERVER_COORDINATOR", "CHAT_TOOL"]
    assert result.tools[0].result["password"] == "[REDACTED]"
    assert result.usage.input_tokens == 10 and result.usage.total_tokens is None
    assert not result.model_execution_verified and result.source_type == "PROTOCOL_DOUBLE"


def test_explicit_prerequisite_turns_recorded_and_unique():
    result, protocol = run(preconditions=["显式前置一", "显式前置二"])
    assert [turn["content"] for turn in protocol.turns] == ["显式前置一", "显式前置二", "目标问题"]
    assert len({turn["turnId"] for turn in protocol.turns}) == 3
    assert len(result.prerequisites) == 2 and len(protocol.saved) == 3


def test_independent_cases_do_not_reuse_sessions_or_conversations():
    first, p1 = run()
    second, p2 = run()
    assert first.conversation_id != second.conversation_id
    assert p1.first_cookie == p2.first_cookie == "JSESSIONID=anon"


def test_same_adapter_object_still_creates_new_http_and_business_sessions():
    protocol = PrinterProtocol()
    adapter = PrinterHttpAdapter(ConnectionConfig(base_url="http://printer.test", source_type="PROTOCOL_DOUBLE"), transport=httpx.MockTransport(protocol),
        environment={"PRINTER_USERNAME": "test-user", "PRINTER_PASSWORD": "private-pass"})
    first = adapter.run("第一题", protocol.saved.append, order_id=7)
    second = adapter.run("第二题", protocol.saved.append, order_id=7)
    assert first.conversation_id != second.conversation_id and first.trace_ref != second.trace_ref
    assert len(protocol.turns) == 2 and second.prerequisites == []
    assert protocol.first_cookie == "JSESSIONID=anon" and protocol.csrf_calls == 2


def test_order_denial_stops_before_conversation():
    protocol = PrinterProtocol()
    protocol.behavior = "order_denied"
    result, protocol = run(protocol)
    assert result.error_code == "ORDER_ACCESS_REJECTED_HTTP_404"
    assert not protocol.turns and not protocol.reads


def test_timeout_never_replays_agent():
    protocol = PrinterProtocol()
    protocol.behavior = "turn_timeout"
    result, protocol = run(protocol)
    assert result.agent_status == "TIMEOUT" and len(protocol.turns) == 1 and protocol.reads == 0


@pytest.mark.parametrize("behavior,reason", [("missing", "MISSING"), ("expired", "EXPIRED"), ("disabled", "DISABLED"), ("write_failed", "WRITE_FAILED"), ("mismatch", "OBSERVATION_CORRELATION_MISMATCH"), ("out_of_order", "INVALID_OBSERVATION_CONTRACT")])
def test_missing_and_invalid_observation_keep_original_answer(behavior, reason):
    protocol = PrinterProtocol()
    protocol.behavior = behavior
    result, protocol = run(protocol)
    assert result.agent_status == "SUCCEEDED" and result.answer == "根据资料处理"
    assert result.observation_status == "UNAVAILABLE" and result.observation_reason == reason
    assert len(protocol.turns) == 1 and result.tools is None


def test_late_observation_get_only_then_imports():
    protocol = PrinterProtocol()
    protocol.behavior = "late"
    # Windows调度不保证两次GET在6ms内完成；验证轮询语义，不测机器性能。
    result, protocol = run(protocol, observation_wait_seconds=0.2)
    assert result.observation_status == "COMPLETE" and protocol.reads == 2 and len(protocol.turns) == 1


def test_partial_observation_deadline_keeps_partial_tools():
    protocol = PrinterProtocol()
    protocol.behavior = "partial"
    result, protocol = run(protocol)
    assert result.observation_status == "PARTIAL" and result.tools_completeness == "PARTIAL"
    assert len(protocol.turns) == 1 and result.tools


@pytest.mark.parametrize("behavior,expected", [("unknown", None), ("empty", [])])
def test_unknown_and_empty_tools_remain_distinct(behavior, expected):
    protocol = PrinterProtocol()
    protocol.behavior = behavior
    result, _ = run(protocol)
    assert result.tools == expected


def test_replay_never_counts_old_usage_as_new():
    protocol = PrinterProtocol()
    protocol.behavior = "replay"
    result, _ = run(protocol)
    assert result.replayed and result.usage.status == "NOT_REQUESTED" and result.usage.input_tokens is None


def test_save_failure_stops_before_observation_and_no_replay():
    protocol = PrinterProtocol()
    adapter = PrinterHttpAdapter(ConnectionConfig(base_url="http://printer.test"), transport=httpx.MockTransport(protocol),
        environment={"PRINTER_USERNAME": "test-user", "PRINTER_PASSWORD": "private-pass"})
    def fail(_):
        raise ValueError("database down")
    with pytest.raises(PersistenceFailure):
        adapter.run("问题", fail, order_id=7)
    assert len(protocol.turns) == 1 and protocol.reads == 0


def test_budget_rejected_before_any_network_call():
    protocol = PrinterProtocol()
    adapter = PrinterHttpAdapter(ConnectionConfig(base_url="http://printer.test", max_agent_calls=1), transport=httpx.MockTransport(protocol))
    with pytest.raises(ValueError, match="AGENT_CALL_BUDGET_EXCEEDED"):
        adapter.run("问题", protocol.saved.append, order_id=7, preconditions=["先问"])
    assert not protocol.calls


def test_technical_failure_preserves_reply_but_execution_failed():
    protocol = PrinterProtocol()
    protocol.behavior = "model_failed"
    result, _ = run(protocol)
    assert result.agent_status == "FAILED" and result.business_status == "MODEL_UNAVAILABLE" and result.answer
