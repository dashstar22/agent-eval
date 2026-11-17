"""评分服务到持久化、HTTP读取的集成检查；无模型请求。"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agenteval.db import Base, make_engine, session_factory
from agenteval.evaluation_models import JudgeAttemptRecord, JudgeResult
from agenteval.main import create_app
from agenteval.models import ExecutionResult
from agenteval.persistence import CaseRun, ScoreSet
from agenteval.schemas import RuleProfile


@pytest.fixture(params=["sqlite"] + (["mysql"] if os.environ.get("AGENTEVAL_TEST_DATABASE_URL") else []))
def scoring_api(request):
    connection = None
    if request.param == "sqlite":
        engine = make_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        factory = session_factory(engine)
    else:
        engine = make_engine(os.environ["AGENTEVAL_TEST_DATABASE_URL"])
        assert engine.url.database.startswith("agenteval_")
        connection = engine.connect()
        transaction = connection.begin()
        factory = sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    # 此处验证只读评分查询；运行生命周期由独立 runtime 测试覆盖。
    app = create_app(engine, runtime_enabled=False)
    app.state.session_factory = factory
    with TestClient(app) as client:
        yield client, factory
    if connection is not None:
        transaction.rollback()
        connection.close()
    engine.dispose()


def frozen(client):
    dataset = client.post("/api/v1/datasets", json={"name": "评分API合成夹具"}).json()
    config = client.post("/api/v1/agent-configs", json={"name": "模拟夹具", "adapter_type": "MOCK"}).json()
    response = client.post(f"/api/v1/datasets/{dataset['id']}/cases", json={
        "case_key": "scoring-api-case", "question": "是否需要补充型号？", "expected_answer": "应追问型号"})
    assert response.status_code == 201
    response = client.post("/api/v1/frozen-runs", json={"dataset_id": dataset["id"], "agent_config_id": config["id"]})
    assert response.status_code == 201
    return response.json()["id"]


def test_frozen_cases_remain_undetermined_without_calls(scoring_api):
    client, _ = scoring_api
    run_id = frozen(client)
    response = client.get(f"/api/v1/runs/{run_id}/scoring-summary")
    assert response.status_code == 200
    summary = response.json()["summary"]
    assert (summary["N"], summary["P"], summary["F"], summary["U"]) == (1, 0, 0, 1)
    assert summary["effective_pass_rate"] is None
    cases = client.get(f"/api/v1/runs/{run_id}/cases").json()["items"]
    detail = client.get(f"/api/v1/case-runs/{cases[0]['id']}/scoring").json()
    assert detail["evaluation"] is None and detail["judge_attempts"] == []


def test_saved_execution_failure_survives_judge_error_and_http(scoring_api):
    from agenteval.evaluation_store import append_judge_attempt, save_assessment, save_execution
    from agenteval.rules import combine_metrics, evaluate_rules

    client, factory = scoring_api
    run_id = frozen(client)
    result = ExecutionResult(agent_status="TIMEOUT", answer=None, started_at="2026-11-16T00:00:00+00:00",
                             ended_at="2026-11-16T00:00:01+00:00", duration_ms=1000,
                             source_type="PROTOCOL_DOUBLE", error_code="AGENT_TIMEOUT")
    judge = JudgeResult(status="ERROR", error_code="AUTH_ERROR", attempts=[JudgeAttemptRecord(
        attempt=1, status="ERROR", response={"raw": "Authorization: Bearer do-not-leak"},
        error={"code": "AUTH_ERROR"})])
    with factory.begin() as session:
        case = session.scalar(select(CaseRun).where(CaseRun.run_id == run_id))
        score_set = session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id))
        case_id, score_set_id = case.id, score_set.id
        save_execution(session, case_id, result)
    with factory.begin() as session:
        case = session.get(CaseRun, case_id)
        append_judge_attempt(session, run_id, score_set_id, case_id, judge.attempts[0])
        rules = RuleProfile()
        assessment = combine_metrics(dict(case.case_snapshot), rules,
                                     evaluate_rules(dict(case.case_snapshot), result, rules), judge)
        save_assessment(session, run_id, score_set_id, case_id, assessment)
    detail = client.get(f"/api/v1/case-runs/{case_id}/scoring")
    assert detail.status_code == 200 and "do-not-leak" not in detail.text
    data = detail.json()
    assert data["agent_status"] == "TIMEOUT" and data["evaluation"]["conclusion"] == "FAIL"
    assert len(data["judge_attempts"]) == 1
    summary = client.get(f"/api/v1/runs/{run_id}/scoring-summary").json()["summary"]
    assert (summary["N"], summary["P"], summary["F"], summary["U"]) == (1, 0, 1, 0)


def test_score_set_must_belong_to_requested_run(scoring_api):
    client, factory = scoring_api
    first, second = frozen(client), frozen(client)
    with factory() as session:
        wrong = session.scalar(select(ScoreSet.id).where(ScoreSet.run_id == second))
    response = client.get(f"/api/v1/runs/{first}/scoring-summary?score_set_id={wrong}")
    assert response.status_code == 404 and response.json()["code"] == "RESOURCE_NOT_FOUND"
    response = client.get("/api/v1/runs/not-a-uuid/scoring-summary")
    assert response.status_code == 422
