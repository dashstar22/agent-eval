import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from agenteval.db import Base, make_engine, session_factory
from agenteval.main import create_app
from agenteval.persistence import AgentConfig, CaseRun, EvaluationScore, Experiment, ScoreSet
from agenteval.privacy import bounded_raw, sanitize
from agenteval.services import save_raw_result
from agenteval.snapshots import digest


@pytest.fixture(params=["sqlite"] + (["mysql"] if os.environ.get("AGENTEVAL_TEST_DATABASE_URL") else []))
def api(request):
    if request.param == "sqlite":
        engine = make_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        connection = None
        factory = session_factory(engine)
    else:
        engine = make_engine(os.environ["AGENTEVAL_TEST_DATABASE_URL"])
        assert engine.url.database.startswith("agenteval_")
        connection = engine.connect()
        transaction = connection.begin()
        factory = sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    # 管理接口回归不启动后台进程，也不恢复共享测试库中的运行。
    app = create_app(engine, runtime_enabled=False)
    app.state.session_factory = factory
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, factory
    if connection is not None:
        transaction.rollback()
        connection.close()
    engine.dispose()


def inputs(client):
    dataset = client.post("/api/v1/datasets", json={"name": "合成数据"}).json()
    config = client.post("/api/v1/agent-configs", json={"name": "模拟", "adapter_type": "MOCK"}).json()
    for key in ["A", "B", "C"]:
        response = client.post(f"/api/v1/datasets/{dataset['id']}/cases", json={"case_key": key, "question": f"{key}问题", "expected_answer": "预期行为", "tags": ["连接"], "enabled": key != "C"})
        assert response.status_code == 201, response.text
    return dataset["id"], config["id"]


def freeze(client, dataset_id, config_id, **kwargs):
    return client.post("/api/v1/frozen-runs", json={"dataset_id": dataset_id, "agent_config_id": config_id, **kwargs})


def test_dataset_and_case_crud_page_filter_location(api):
    client, _ = api
    dataset_id, _ = inputs(client)
    base = f"/api/v1/datasets/{dataset_id}"
    assert client.patch(base, json={"name": "修改后"}).json()["name"] == "修改后"
    assert client.get(base).json()["enabled_cases"] == 2
    page = client.get(base + "/cases?page=2&page_size=1").json()
    assert page["total"] == 3 and page["items"][0]["case_key"] == "B"
    assert client.get(base + "/cases?enabled=true&tag=连接").json()["total"] == 2
    assert client.get(base + "/cases?tag=连").json()["total"] == 0
    update = client.patch(base + "/cases/A", json={"question": "新问题", "enabled": False, "tags": ["卡纸"]})
    assert update.status_code == 200 and update.json()["case_key"] == "A"
    assert client.get(base + "/cases/A").json()["question"] == "新问题"
    assert client.delete(base + "/cases/A").status_code == 204
    assert client.get(base + "/cases/A").status_code == 404
    assert client.post(base + "/cases", json={"case_key": "A", "question": "问", "expected_answer": "答"}).status_code == 409


@pytest.mark.parametrize("patch", [{"question": " "}, {"expected_answer": ""}, {"required_metrics": []}, {"tool_match_mode": "bad"}, {"expected_tools": []}, {"case_key": "changed"}, {"enabled": None}, {"preconditions": {"password": "DO_NOT_LEAK"}}])
def test_case_validation_rolls_back_and_keeps_stable_key(api, patch):
    client, _ = api
    dataset_id, _ = inputs(client)
    url = f"/api/v1/datasets/{dataset_id}/cases/A"
    response = client.patch(url, json=patch)
    assert response.status_code == 422, response.text
    assert "DO_NOT_LEAK" not in response.text
    assert client.get(url).json()["question"] == "A问题"


def test_tool_null_empty_and_patch_combination(api):
    client, _ = api
    dataset_id, _ = inputs(client)
    url = f"/api/v1/datasets/{dataset_id}/cases/A"
    assert client.patch(url, json={"expected_tools": [], "tool_match_mode": "exact"}).status_code == 200
    assert client.patch(url, json={"tool_match_mode": "contains"}).status_code == 422
    assert client.get(url).json()["tool_match_mode"] == "exact"
    assert client.patch(url, json={"expected_tools": None, "tool_match_mode": "contains"}).json()["expected_tools"] is None


def test_case_key_database_length_boundary(api):
    client, _ = api
    dataset_id, _ = inputs(client)
    base = f"/api/v1/datasets/{dataset_id}/cases"
    for length, status in [(100, 201), (101, 422)]:
        response = client.post(base, json={"case_key": "X" * length, "question": "问", "expected_answer": "答"})
        assert response.status_code == status
        if status == 201:
            assert response.headers["Location"].endswith("X" * length)


def test_empty_enabled_and_disabled_config_refused(api):
    client, _ = api
    dataset_id, config_id = inputs(client)
    run = freeze(client, dataset_id, config_id).json()
    assert run["total_cases"] == 2 and run["status"] == "FROZEN" and run["execution_available"] is False
    client.patch(f"/api/v1/agent-configs/{config_id}", json={"enabled": False})
    assert freeze(client, dataset_id, config_id).json()["code"] == "AGENT_CONFIG_DISABLED"
    assert client.get(f"/api/v1/runs/{run['id']}").status_code == 200
    client.patch(f"/api/v1/agent-configs/{config_id}", json={"enabled": True})
    for key in ["A", "B"]:
        client.patch(f"/api/v1/datasets/{dataset_id}/cases/{key}", json={"enabled": False})
    assert freeze(client, dataset_id, config_id).json()["code"] == "NO_ENABLED_CASES"


def test_snapshot_digest_and_history_after_edits_and_deletes(api):
    client, factory = api
    dataset_id, config_id = inputs(client)
    original = freeze(client, dataset_id, config_id).json()
    run_url = f"/api/v1/runs/{original['id']}"
    case = client.get(run_url + "/cases").json()["items"][0]
    with factory.begin() as session:
        score = session.scalar(select(ScoreSet).where(ScoreSet.run_id == original["id"]))
        session.add(EvaluationScore(run_id=original["id"], score_set_id=score.id, case_run_id=case["id"], metric="correctness", status="PASS", score=4, reason="仅验证存储的测试记录"))
    client.patch(f"/api/v1/datasets/{dataset_id}/cases/A", json={"question": "改写", "expected_answer": "改写参考", "enabled": False})
    client.patch(f"/api/v1/agent-configs/{config_id}", json={"name": "改写配置"})
    newer = freeze(client, dataset_id, config_id).json()
    assert newer["total_cases"] == 1 and newer["snapshot_sha256"] != original["snapshot_sha256"]
    client.delete(f"/api/v1/datasets/{dataset_id}/cases/A")
    client.delete(f"/api/v1/datasets/{dataset_id}")
    client.delete(f"/api/v1/agent-configs/{config_id}")
    current = client.get(run_url).json()
    assert current["snapshot"] == original["snapshot"]
    assert current["snapshot_sha256"] == digest(current["snapshot"]) == original["snapshot_sha256"]
    assert current["total_cases"] == 2
    old_case = client.get(f"/api/v1/case-runs/{case['id']}").json()
    assert old_case["case_snapshot"]["question"] == "A问题" and old_case["scores"][0]["score"] == 4
    assert client.get(run_url + "/report-input").json()["score_sets"][0]["kind"] == "ORIGINAL"
    assert freeze(client, dataset_id, config_id).status_code == 404


def test_credentials_never_exposed_in_lists_errors_or_snapshots(api, monkeypatch):
    client, factory = api
    dataset_id, _ = inputs(client)
    monkeypatch.setenv("SPECIAL_CREDENTIAL", "super-private-canary")
    monkeypatch.setenv("JUDGE_CREDENTIAL", "judge-private-canary")
    body = {"name": "HTTP", "adapter_type": "HTTP", "base_url": "http://127.0.0.1:9999/agent", "source_type": "PROTOCOL_DOUBLE", "credential_refs": {"bearer_ref": "SPECIAL_CREDENTIAL"}}
    created = client.post("/api/v1/agent-configs", json=body)
    config_id = created.json()["id"]
    assert created.status_code == 201 and created.json()["credential_refs"]["bearer_ref"] == "SPECIAL_CREDENTIAL"
    assert client.get(f"/api/v1/agent-configs/{config_id}").json()["credential_status"]["bearer_ref"] == "RESOLVED"
    bad = client.patch(f"/api/v1/agent-configs/{config_id}", json={"base_url": "http://name:super-private-canary@localhost"})
    assert bad.status_code == 422 and "super-private-canary" not in bad.text
    run = freeze(client, dataset_id, config_id, judge_profile={"credential_ref": "JUDGE_CREDENTIAL"}, version_label="judge-private-canary super-private-canary").json()
    serialized = json.dumps(run)
    assert "super-private-canary" not in serialized and "judge-private-canary" not in serialized
    assert "super-private-canary" not in client.get("/api/v1/agent-configs").text
    assert "super-private-canary" not in client.post("/api/v1/datasets", json={"name": "bad", "super-private-canary": "anything"}).text
    assert "super-private-canary" not in client.post("/api/v1/agent-configs", json={"name": "bad", "adapter_type": "MOCK", "expected_version": {"super-private-canary": 42}}).text
    monkeypatch.delenv("SPECIAL_CREDENTIAL")
    missing = freeze(client, dataset_id, config_id)
    assert missing.status_code == 422 and missing.json()["code"] == "CREDENTIAL_REFERENCE_UNRESOLVED"


def test_raw_output_double_redaction_and_limits(api):
    client, factory = api
    dataset_id, config_id = inputs(client)
    run = freeze(client, dataset_id, config_id).json()
    case_id = client.get(f"/api/v1/runs/{run['id']}/cases").json()["items"][0]["id"]
    raw = {"api_key": "new-unknown-key", "output": "Authorization: Bearer unknown-auth\nCookie=session=unknown-cookie\nX-API-Key: unknown-key\n邮箱 alice@example.com 电话13812345678 http://name:undisclosed@localhost", "input_tokens": 7}
    with factory.begin() as session:
        save_raw_result(session, session.get(CaseRun, case_id), raw)
    response = client.get(f"/api/v1/case-runs/{case_id}")
    for private in ["new-unknown-key", "unknown-auth", "unknown-cookie", "unknown-key", "alice@example.com", "13812345678", "undisclosed"]:
        assert private not in response.text
    assert response.json()["raw_agent"]["data"]["input_tokens"] == 7
    clipped = bounded_raw({"result": "测" * 1000}, max_bytes=128)
    assert clipped["completeness"] == "PARTIAL" and clipped["reason"] == "SIZE_LIMIT"
    nested = value = {}
    for _ in range(100):
        value["nested"] = {}
        value = value["nested"]
    assert "TRUNCATED_DEPTH" in json.dumps(sanitize(nested))


def test_uniform_errors_and_resource_location(api):
    client, _ = api
    for url, status in [("/api/v1/datasets?page=0", 422), ("/api/v1/datasets?page_size=101", 422), ("/api/v1/datasets/999", 404), ("/api/v1/runs/../invalid", 404), ("/api/v1/runs/not-uuid", 422), ("/unknown", 404)]:
        response = client.get(url)
        assert response.status_code == status
        assert set(response.json()) == {"code", "message", "trace_id", "fields"}
        assert response.headers["X-Trace-Id"] == response.json()["trace_id"]
    response = client.post("/api/v1/datasets", json={"name": "新增"})
    assert client.get(response.headers["Location"]).json()["id"] == response.json()["id"]


def test_dynamic_raw_keys_and_experiment_label_are_redacted(api, monkeypatch):
    client, factory = api
    monkeypatch.setenv("JUDGE_API_KEY", "private-key-canary-raw")
    dataset_id, config_id = inputs(client)
    response = freeze(client, dataset_id, config_id, version_label="candidate private-key-canary-raw")
    assert response.status_code == 201
    run = response.json()
    with factory.begin() as session:
        experiment = session.get(Experiment, run["experiment_id"])
        assert experiment.version_label == "candidate [REDACTED]"
        case = session.scalar(select(CaseRun).where(CaseRun.run_id == run["id"]))
        save_raw_result(session, case, {"private-key-canary-raw": "value", "alice@example.com": "value"})
        case_id = case.id
    shown = client.get(f"/api/v1/case-runs/{case_id}").text
    assert "private-key-canary-raw" not in shown and "alice@example.com" not in shown


def test_precise_usage_and_budget_counter_allowlist():
    safe_fields = {"known_input_tokens_records": 2, "known_output_tokens_records": 1,
                   "known_total_tokens_records": 1, "unit_tokens": 1000000,
                   "max_tokens": 2000, "max_completion_tokens": 3000,
                   "input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    payload = safe_fields | {"access_token": "private-auth", "api_key": "private-key", "token_body": "private-token"}
    sanitized = sanitize(payload)
    assert {key: sanitized[key] for key in safe_fields} == safe_fields
    assert all(sanitized[key] == "[REDACTED]" for key in ["access_token", "api_key", "token_body"])
