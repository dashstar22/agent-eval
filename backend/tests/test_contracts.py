import json

import httpx
import pytest
from pydantic import ValidationError

from agenteval.adapters import HttpAdapter, MockAdapter
from agenteval.config import ConnectionConfig
from agenteval.models import ExecutionResult, Usage


def config(**kwargs):
    return ConnectionConfig(base_url="http://unit.test/agent", source_type="PROTOCOL_DOUBLE", **kwargs)


def test_import_and_mock_source_survives_serialization():
    result = MockAdapter().run("测试")
    reloaded = ExecutionResult.model_validate_json(result.model_dump_json())
    assert reloaded.source_type == "MOCK" and not reloaded.model_execution_verified
    assert reloaded.tools == [] and reloaded.usage.total_tokens is None


@pytest.mark.parametrize("value", [{"status": "COMPLETE", "input_tokens": 3}, {"status": "UNAVAILABLE", "total_tokens": 0}, {"input_tokens": -1}])
def test_invalid_usage_rejected(value):
    with pytest.raises(ValidationError):
        Usage.model_validate(value)


@pytest.mark.parametrize("url", ["file:///tmp/a", "http://name:secret@unit.test/", "https://unit.test/?token=abc"])
def test_config_rejects_secret_urls(url):
    with pytest.raises(ValidationError):
        ConnectionConfig(base_url=url)


def test_reference_not_secret_value_and_missing_ref_safe():
    setting = config(password_ref="PRIVATE_PASSWORD")
    assert "hidden-secret" not in setting.model_dump_json()
    with pytest.raises(ValueError, match="CREDENTIAL_REFERENCE_UNRESOLVED"):
        setting.credentials({})


@pytest.mark.parametrize("tools,completeness", [(None, "UNAVAILABLE"), ([], "COMPLETE")])
def test_generic_unknown_tools_differ_from_proven_empty(tools, completeness):
    sent = []
    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"answer": "事实", "tools": tools, "tools_completeness": completeness,
            "usage": {"status": "PARTIAL", "input_tokens": 9, "reason": "OUTPUT_UNKNOWN"}, "password": "secret"})
    result = HttpAdapter(config(), transport=httpx.MockTransport(handler)).run("问题", expected_answer="gold", expected_tools=["searchKnowledge"])
    assert result.agent_status == "SUCCEEDED"
    assert result.tools == tools and result.tools_completeness == completeness
    assert result.usage.input_tokens == 9 and result.usage.output_tokens is None
    assert sent[0]["question"] == "问题" and "gold" not in json.dumps(sent)
    assert result.raw_agent["response"]["password"] == "[REDACTED]"
    assert result.source_type == "PROTOCOL_DOUBLE"


@pytest.mark.parametrize("response", [httpx.Response(200, json={"answer": 42}), httpx.Response(200, content="bad"), httpx.Response(200, json=[]), httpx.Response(500, json={"message": "failure"})])
def test_generic_invalid_contract_and_http_error(response):
    result = HttpAdapter(config(), transport=httpx.MockTransport(lambda req: response)).run("问题")
    assert result.agent_status == "PROTOCOL_ERROR" and result.answer is None


def test_generic_timeout_not_replayed():
    calls = []
    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("untrusted error with secret", request=req)
    result = HttpAdapter(config(), transport=httpx.MockTransport(handler)).run("问题")
    assert len(calls) == 1 and result.agent_status == "TIMEOUT"
    assert result.usage.total_tokens is None and "secret" not in result.model_dump_json()


def test_generic_response_bound():
    result = HttpAdapter(config(response_max_bytes=256), transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"answer": "x" * 512}))).run("q")
    assert result.error_code == "RESPONSE_TOO_LARGE"


def test_generic_all_normalized_fields_redact_bearer_and_personal_data():
    secret = "private-credential-123"
    text = f"{secret} somebody@example.com 13800138000"
    def handler(req):
        assert req.headers["authorization"] == f"Bearer {secret}"
        return httpx.Response(200, json={"answer": text, "tools": [{"event_id": "e", "sequence": 1, "source": "CHAT_TOOL", "name": "search", "status": "HIT", "result": {"value": text, "authorization": secret}}],
            "contexts": [{"text": text}], "usage": {"status": "PARTIAL", "input_tokens": 10}})
    result = HttpAdapter(config(bearer_ref="KEY"), environment={"KEY": secret}, transport=httpx.MockTransport(handler)).run("q")
    assert result.agent_status == "SUCCEEDED" and result.usage.input_tokens == 10
    serialized = result.model_dump_json()
    assert secret not in serialized and "somebody@example.com" not in serialized and "13800138000" not in serialized
