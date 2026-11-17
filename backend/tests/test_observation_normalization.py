from uuid import uuid4

import pytest

from agenteval.adapters import PrinterHttpAdapter
from agenteval.config import ConnectionConfig
from agenteval.models import ExecutionResult


def input_record():
    result = ExecutionResult(agent_status="SUCCEEDED", answer="fixture", started_at="2026-11-16T00:00:00Z", ended_at="2026-11-16T00:00:01Z",
        duration_ms=1, source_type="REAL_EXTERNAL", trace_ref=str(uuid4()), conversation_id=str(uuid4()), turn_id=str(uuid4()), order_id=1)
    data = {"traceId": result.trace_ref, "conversationId": result.conversation_id, "turnId": result.turn_id, "orderId": 1, "complete": True,
        "events": [], "contexts": [], "contextScope": "NOT_USED", "contextCompleteness": "COMPLETE", "sourceType": "HTTP_AGENT", "modelMode": "REMOTE_API_UNVERIFIED",
        "usage": {"status": "UNAVAILABLE", "attempts": [{"attempt": 1, "modelId": "actual-model", "source": "MODEL_RESPONSE_METADATA", "status": "SUCCEEDED"}]}}
    return result, data


def normalize(result, data, **kwargs):
    return PrinterHttpAdapter(ConnectionConfig(base_url="http://127.0.0.1", **kwargs))._import(result, data, ())


@pytest.mark.parametrize("source,mode", [("PROTOCOL_SIMULATION", "PROTOCOL_SIMULATION"), ("HTTP_AGENT", "PROTOCOL_SIMULATION"),
    ("PROTOCOL_SIMULATION", "REMOTE_API_UNVERIFIED"), ("MOCK", "REMOTE_API_UNVERIFIED")])
def test_simulation_source_or_mode_overrides_real_configuration(source, mode):
    result, data = input_record()
    data.update(sourceType=source, modelMode=mode)
    actual = normalize(result, data)
    assert actual.source_type == "PROTOCOL_DOUBLE" and not actual.model_execution_verified
    assert actual.raw_agent["source_type"] == "PROTOCOL_DOUBLE"


@pytest.mark.parametrize("model", ["UNKNOWN", "unknown", " ", "UNAVAILABLE", "NULL", None])
def test_unknown_model_id_cannot_verify_real_execution(model):
    result, data = input_record()
    data["usage"]["attempts"][0]["modelId"] = model
    assert not normalize(result, data).model_execution_verified


@pytest.mark.parametrize("source,mode", [("NO_MODEL_REQUEST", "REMOTE_API_UNVERIFIED"), ("HTTP_AGENT", "DISABLED"), ("UNKNOWN", "REMOTE_API_UNVERIFIED"),
    ("HTTP_AGENT", "LOCAL_API_UNVERIFIED")])
def test_missing_or_conflicting_execution_provenance_is_not_verified(source, mode):
    result, data = input_record()
    data.update(sourceType=source, modelMode=mode)
    assert not normalize(result, data).model_execution_verified


def test_real_metadata_verification_and_configured_double_never_upgrades():
    result, data = input_record()
    assert normalize(result, data).model_execution_verified
    assert not normalize(result, data, source_type="PROTOCOL_DOUBLE").model_execution_verified


def full_context():
    return {"pointId": "p1", "content": "安全步骤", "text": "安全步骤", "title": "演示手册", "source": "演示手册", "version": "v1",
        "simulated": True, "useCondition": "切断电源", "models": ["DEMO"], "modelScope": "EXACT", "pageNumber": 4, "section": "维护"}


def test_all_actual_context_fields_and_unknown_extensions_survive_import():
    result, data = input_record()
    context = {**full_context(), "futureField": {"value": [1, "kept"]}}
    data.update(contexts=[context], contextScope="ACTUAL_MODEL_CONTEXT")
    actual = normalize(result, data)
    stored = actual.contexts[0]
    assert stored.point_id == "p1" and stored.content == "安全步骤" and stored.title == "演示手册"
    assert stored.use_condition == "切断电源" and stored.models == ["DEMO"] and stored.model_scope == "EXACT"
    assert stored.page_number == 4 and stored.section == "维护" and stored.simulated is True
    assert stored.metadata == context and actual.context_completeness == "COMPLETE"


@pytest.mark.parametrize("missing", ["pointId", "useCondition", "models", "modelScope", "pageNumber", "section"])
def test_missing_actual_context_fields_downgrade_complete(missing):
    result, data = input_record()
    context = full_context()
    del context[missing]
    data.update(contexts=[context], contextScope="ACTUAL_MODEL_CONTEXT")
    actual = normalize(result, data)
    assert actual.context_completeness == "PARTIAL" and actual.observation_status == "PARTIAL"
    assert actual.observation_reason == "CONTEXT_FIELDS_MISSING"


def test_import_redaction_preserves_structure_and_downgrades_context():
    result, data = input_record()
    data.update(contexts=[{**full_context(), "useCondition": "联系 demo@example.com"}], contextScope="ACTUAL_MODEL_CONTEXT")
    actual = normalize(result, data)
    assert "demo@example.com" not in actual.model_dump_json()
    assert actual.context_completeness == "PARTIAL" and actual.observation_reason == "CONTEXT_REDACTED_ON_IMPORT"


def test_attempt_template_versions_and_per_attempt_metadata_are_retained():
    result, data = input_record()
    data["usage"]["attempts"][0].update(requestedModelId="model-alias", promptSha256="a" * 64, durationMs=23, availability="UNAVAILABLE")
    data["observedVersion"] = {"promptSha256": "a" * 64, "promptSha256s": ["a" * 64, "b" * 64]}
    actual = normalize(result, data)
    assert actual.usage.attempts[0].prompt_sha256 == "a" * 64 and actual.usage.attempts[0].duration_ms == 23
    assert actual.usage.attempts[0].requested_model_id == "model-alias" and actual.observed_version.prompt_sha256s == ["a" * 64, "b" * 64]
