from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from agenteval.db import Base, make_engine, session_factory
from agenteval.main import create_app
from agenteval.persistence import CaseRun, EvaluationRun, RuntimeControl, ScoreSet
from test_runtime_control import HoldingExecutor


@pytest.fixture
def api(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    app = create_app(engine, runtime_executor_factory=HoldingExecutor)
    with TestClient(app, raise_server_exceptions=False) as client:
        dataset = client.post("/api/v1/datasets", json={"name": "后台输入"}).json()
        config = client.post("/api/v1/agent-configs", json={"name": "模拟", "adapter_type": "MOCK"}).json()
        for key in ["A", "B", "DISABLED"]:
            response = client.post(f"/api/v1/datasets/{dataset['id']}/cases", json={
                "case_key": key, "question": "合成问题", "expected_answer": "参考行为", "enabled": key != "DISABLED"})
            assert response.status_code == 201
        payload = {"request_id": str(uuid4()), "dataset_id": dataset["id"], "agent_config_id": config["id"]}
        yield client, app, payload
    engine.dispose()


def test_accept_has_all_cases_before_submit_idempotent_and_fixed_plan(api):
    client, app, payload = api
    first = client.post("/api/v1/runs", json=payload)
    assert first.status_code == 202, first.text
    result = first.json()
    assert result["status"] == "QUEUED" and result["total_cases"] == 2
    assert first.headers["Location"].endswith(result["run_id"])
    assert len(app.state.runtime_manager.executor.jobs) == 1
    client.patch(f"/api/v1/datasets/{payload['dataset_id']}/cases/A", json={"enabled": False, "question": "后来编辑"})
    replay = client.post("/api/v1/runs", json=payload)
    assert replay.status_code == 202 and replay.json()["idempotent_replay"] is True
    assert replay.json()["run_id"] == result["run_id"]
    assert len(app.state.runtime_manager.executor.jobs) == 1
    detail = client.get(first.headers["Location"]).json()
    assert detail["progress"] == {"total": 2, "executed": 0, "evaluated": 0, "remaining": 2, "complete": False}
    assert {key: detail["summary"][key] for key in ("N", "P", "F", "U")} == {"N": 2, "P": 0, "F": 0, "U": 2}
    assert detail["summary"]["provisional"] is True
    cases = client.get(first.headers["Location"] + "/cases").json()
    assert cases["total"] == 2 and all(row["case_snapshot"]["question"] == "合成问题" for row in cases["items"])


def test_same_id_different_payload_refused_and_busy_returns_active_id(api):
    client, app, payload = api
    accepted = client.post("/api/v1/runs", json=payload).json()
    mismatch = client.post("/api/v1/runs", json=payload | {"version_label": "新标签"})
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "REQUEST_ID_PAYLOAD_MISMATCH"
    busy = client.post("/api/v1/runs", json=payload | {"request_id": str(uuid4())})
    assert busy.status_code == 409 and busy.json()["code"] == "RUNTIME_BUSY"
    assert busy.json()["fields"][0]["value"] == accepted["run_id"]
    assert len(app.state.runtime_manager.executor.jobs) == 1


def test_concurrent_start_only_one_accepted(api):
    client, app, payload = api
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.post, "/api/v1/runs", json=payload | {"request_id": str(uuid4())}) for _ in range(2)]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [202, 409]
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EvaluationRun)) == 1
        assert session.scalar(select(func.count()).select_from(CaseRun)) == 2
    assert len(app.state.runtime_manager.executor.jobs) == 1


def test_plan_save_failure_has_no_job_or_partial_run(api, monkeypatch):
    client, app, payload = api
    from agenteval import runtime_control
    original = runtime_control.freeze_run

    def fail(session, *args, **kwargs):
        original(session, *args, **kwargs)
        raise SQLAlchemyError("secret-must-not-leak")

    monkeypatch.setattr(runtime_control, "freeze_run", fail)
    result = client.post("/api/v1/runs", json=payload)
    assert result.status_code == 503 and result.json()["code"] == "DATABASE_ERROR"
    assert "secret-must-not-leak" not in result.text
    assert app.state.runtime_manager.executor.jobs == []
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EvaluationRun)) == 0
        assert session.get(RuntimeControl, 1).active_job_id is None


def test_registration_failure_persisted_and_replay_never_retries(api):
    client, app, payload = api
    executor = app.state.runtime_manager.executor

    def fail(*args):
        raise RuntimeError("private-worker-failure")

    executor.submit = fail
    result = client.post("/api/v1/runs", json=payload)
    assert result.status_code == 503 and result.json()["code"] == "RUNTIME_REGISTRATION_FAILED"
    run_id = result.json()["fields"][0]["value"]
    assert "private-worker-failure" not in result.text
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "FAILED"
    replay = client.post("/api/v1/runs", json=payload)
    assert replay.status_code == 202 and replay.json()["status"] == "FAILED"
    with app.state.session_factory() as session:
        assert session.get(RuntimeControl, 1).active_job_id is None
        assert session.scalar(select(ScoreSet.status).where(ScoreSet.run_id == run_id)) == "FAILED"


def test_runtime_requires_lifespan_and_canonical_request_id(api):
    client, app, payload = api
    invalid = client.post("/api/v1/runs", json=payload | {"request_id": "not-a-uuid"})
    assert invalid.status_code == 422
    no_runtime = create_app(app.state.runtime_manager.engine, runtime_enabled=False)
    with TestClient(no_runtime) as other:
        result = other.post("/api/v1/runs", json=payload)
        assert result.status_code == 503 and result.json()["code"] == "RUNTIME_UNAVAILABLE"
        assert other.get("/api/v1/health").json()["execution_available"] is False


def test_real_external_needs_explicit_total_budget(api):
    client, app, payload = api
    config = client.post("/api/v1/agent-configs", json={"name": "真实连接", "adapter_type": "HTTP",
        "base_url": "http://127.0.0.1:1", "source_type": "REAL_EXTERNAL"}).json()
    result = client.post("/api/v1/runs", json=payload | {"agent_config_id": config["id"]})
    assert result.status_code == 422 and result.json()["code"] == "REAL_EXECUTION_BUDGET_REQUIRED"
    assert app.state.runtime_manager.executor.jobs == []
