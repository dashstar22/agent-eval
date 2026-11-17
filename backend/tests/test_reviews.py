"""人工复核与回归候选的持久化验收；全部使用本地合成夹具。"""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from agenteval.db import Base, make_engine, session_factory
from agenteval.main import create_app, get_session
from agenteval.models import Context, ExecutionResult, ToolEvent, now
from agenteval.persistence import CaseRun, EvaluationScore, ScoreSet, TestCase
from agenteval.review_api import add_review_routes
from agenteval.review_models import HumanReview, RegressionDraft
from test_workbench import freeze, record, setup_inputs


@pytest.fixture
def reviews():
    engine = make_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    app = create_app(engine, runtime_enabled=False)
    if not any(getattr(route, "path", "") == "/api/v1/extensions/reviews" for route in app.routes):
        router = APIRouter(prefix="/api/v1")
        add_review_routes(router, get_session)
        app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as client:
        inputs = setup_inputs(client)
        run = freeze(client, inputs)
        case, score = record(factory, run, score=2, attempts=True)
        second, _ = record(factory, run, key="B", judge_error=True)
        target = client.post("/api/v1/datasets", json={"name": "回归候选目标"}).json()["id"]
        yield client, factory, {"run": run, "case": case, "second": second, "score": score, "target": target, "inputs": inputs}
    engine.dispose()


def review_input(ids, **patch):
    return {"case_run_id": ids["case"], "score_set_id": ids["score"], "reviewer_name": "本机复核人",
            "conclusion": "FAIL", "scores": {"correctness": 2, "relevance": 3},
            "reason": "合成复核理由", "human_confirmed": True, **patch}


def draft_input(ids, **patch):
    return {"case_run_id": ids["case"], "score_set_id": ids["score"], "target_dataset_id": ids["target"],
            "case_key": "REG-A", "expected_answer": "人工修订参考行为，不复制失败回答", "reason": "需要补充回归覆盖", **patch}


def test_blind_case_picker_and_case_projection_never_include_judge(reviews):
    client, _, ids = reviews
    result = client.get("/api/v1/extensions/reviews/cases", params={"run_id": ids["run"], "score_set_id": ids["score"]})
    assert result.status_code == 200, result.text
    assert all(set(item) == {"id", "case_key", "question"} for item in result.json()["items"])
    result = client.get(f"/api/v1/extensions/reviews/cases/{ids['case']}", params={"score_set_id": ids["score"]})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["blind"] is True and data["judge"] is None
    assert "合成评分夹具" not in result.text and "conclusion" not in result.text
    assert data["case"]["question"] == "A如何排查卡纸？"
    assert data["case"]["answer"] == "断电后检查纸路"
    assert data["case"]["context_scope"] == "UNAVAILABLE"
    assert "未经身份认证" in data["identity_notice"]
    unblind = client.get(f"/api/v1/extensions/reviews/cases/{ids['case']}", params={"blind": False}).json()
    assert unblind["judge"]["conclusion"] == "FAIL"
    assert any(item["reason"] == "合成评分夹具" for item in unblind["judge"]["metrics"])


def test_review_append_history_latest_and_original_scoring_unchanged(reviews):
    client, factory, ids = reviews
    before = client.get(f"/api/v1/case-runs/{ids['case']}/scoring").json()
    first = client.post("/api/v1/extensions/reviews", json=review_input(ids))
    second = client.post("/api/v1/extensions/reviews", json=review_input(ids, conclusion="PASS", scores={"correctness": 5}))
    assert first.status_code == second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]
    assert second.json()["identity_type"] == "SELF_DECLARED"
    history = client.get("/api/v1/extensions/reviews", params={"run_id": ids["run"]}).json()
    assert history["total"] == 2 and len(history["latest_reviews"]) == 1
    assert history["latest_reviews"][0]["id"] == second.json()["id"]
    assert client.get(f"/api/v1/case-runs/{ids['case']}/scoring").json() == before
    with factory() as session:
        row = session.get(HumanReview, first.json()["id"])
        with pytest.raises(TypeError, match="IMMUTABLE"):
            row.scores["correctness"] = 5
        row.reason = "覆盖历史"
        with pytest.raises(ValueError, match="APPEND_ONLY"):
            session.flush()
        session.rollback()
        row = session.get(HumanReview, first.json()["id"])
        session.delete(row)
        with pytest.raises(ValueError, match="APPEND_ONLY"):
            session.flush()


@pytest.mark.parametrize("patch", [
    {"human_confirmed": False}, {"human_confirmed": 1}, {"human_confirmed": "true"},
    {"scores": {"correctness": True}}, {"scores": {"correctness": 2.0}},
    {"scores": {"correctness": "2"}}, {"scores": {"correctness": 0}},
    {"scores": {"correctness": 6}}, {"scores": {"unknown": 3}},
    {"scores": {"faithfulness": 3}}, {"reviewer_name": " "}, {"reason": " "},
    {"conclusion": "UNKNOWN"}, {"case_run_id": "bad-id"}, {"overwrite": True},
])
def test_review_rejects_unconfirmed_invalid_or_inapplicable_scores(reviews, patch):
    client, factory, ids = reviews
    result = client.post("/api/v1/extensions/reviews", json=review_input(ids, **patch))
    assert result.status_code == 422, result.text
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(HumanReview)) == 0


@pytest.mark.parametrize("scope,completeness,contexts", [
    ("UNAVAILABLE", "UNAVAILABLE", None),
    ("CITATION_SUBSET", "COMPLETE", [Context(text="仅引用片段")]),
    ("ACTUAL_MODEL_CONTEXT", "PARTIAL", [Context(text="不完整的实际片段")]),
    ("ACTUAL_MODEL_CONTEXT", "UNAVAILABLE", None),
    ("ACTUAL_MODEL_CONTEXT", "COMPLETE", []),
    ("NOT_USED", "COMPLETE", []),
])
def test_required_faithfulness_never_overrides_missing_actual_context(reviews, scope, completeness, contexts):
    client, factory, ids = reviews
    assert client.patch(f"/api/v1/datasets/{ids['inputs'][0]}/cases/A", json={
        "required_metrics": ["correctness", "faithfulness"],
    }).status_code == 200
    run = freeze(client, ids["inputs"])
    execution = ExecutionResult(answer="合成回答", agent_status="SUCCEEDED", started_at=now(), ended_at=now(),
        duration_ms=1, source_type="MOCK", context_scope=scope, context_completeness=completeness, contexts=contexts)
    case_id, score_id = record(factory, run, result=execution)
    scope_ids = {**ids, "run": run, "case": case_id, "score": score_id}
    before = client.get(f"/api/v1/case-runs/{case_id}/scoring", params={"score_set_id": score_id}).json()
    metric = next(item for item in before["metrics"] if item["metric"] == "faithfulness")
    assert metric["status"] == "NA" and metric["required"] is True
    view = client.get(f"/api/v1/extensions/reviews/cases/{case_id}", params={"score_set_id": score_id}).json()
    assert "faithfulness" not in view["applicable_metrics"]
    rejected = client.post("/api/v1/extensions/reviews", json=review_input(scope_ids, scores={"faithfulness": 5}))
    assert rejected.status_code == 422 and rejected.json()["code"] == "REVIEW_METRIC_NOT_APPLICABLE"
    # 用户仍可记录整体判断与适用的独立指标；缺失依据不会被改成自动或人工满分。
    accepted = client.post("/api/v1/extensions/reviews", json=review_input(scope_ids, conclusion="UNDETERMINED", scores={"correctness": 4}))
    assert accepted.status_code == 201 and accepted.json()["scores"] == {"correctness": 4}
    assert client.get(f"/api/v1/case-runs/{case_id}/scoring", params={"score_set_id": score_id}).json() == before


def test_complete_actual_context_allows_human_faithfulness_even_when_judge_failed(reviews):
    client, factory, ids = reviews
    run = freeze(client, ids["inputs"])
    execution = ExecutionResult(answer="合成回答", agent_status="SUCCEEDED", started_at=now(), ended_at=now(),
        duration_ms=1, source_type="MOCK", context_scope="ACTUAL_MODEL_CONTEXT", context_completeness="COMPLETE",
        contexts=[Context(text="实际供模型使用的完整合成上下文", version="synthetic-v1")])
    case_id, score_id = record(factory, run, result=execution, judge_error=True)
    scope_ids = {**ids, "run": run, "case": case_id, "score": score_id}
    before = client.get(f"/api/v1/case-runs/{case_id}/scoring", params={"score_set_id": score_id}).json()
    view = client.get(f"/api/v1/extensions/reviews/cases/{case_id}", params={"score_set_id": score_id}).json()
    assert "faithfulness" in view["applicable_metrics"] and view["judge"] is None
    created = client.post("/api/v1/extensions/reviews", json=review_input(scope_ids, scores={"faithfulness": 4}))
    assert created.status_code == 201 and created.json()["scores"] == {"faithfulness": 4}
    assert client.get(f"/api/v1/case-runs/{case_id}/scoring", params={"score_set_id": score_id}).json() == before
    consistency = client.get("/api/v1/extensions/reviews/consistency", params={"run_id": run, "score_set_id": score_id}).json()
    faithfulness = next(item for item in consistency["metrics"] if item["metric"] == "faithfulness")
    assert faithfulness["valid_pair_count"] == 0 and faithfulness["exact_agreement_rate"] is None


def test_unsaved_execution_cannot_enable_required_human_faithfulness(reviews):
    client, _, ids = reviews
    assert client.patch(f"/api/v1/datasets/{ids['inputs'][0]}/cases/A", json={
        "required_metrics": ["faithfulness"],
    }).status_code == 200
    run = freeze(client, ids["inputs"])
    case = client.get(f"/api/v1/runs/{run}/cases").json()["items"][0]
    score_id = client.get(f"/api/v1/runs/{run}/score-sets").json()["items"][0]["id"]
    view = client.get(f"/api/v1/extensions/reviews/cases/{case['id']}", params={"score_set_id": score_id}).json()
    assert "faithfulness" not in view["applicable_metrics"]
    result = client.post("/api/v1/extensions/reviews", json=review_input(ids, case_run_id=case["id"], score_set_id=score_id, scores={"faithfulness": 5}))
    assert result.status_code == 422
    overall = client.post("/api/v1/extensions/reviews", json=review_input(ids, case_run_id=case["id"], score_set_id=score_id, scores={}, conclusion="UNDETERMINED"))
    assert overall.status_code == 201


@pytest.mark.parametrize("agent_status", ["RESULT_UNKNOWN", "TIMEOUT", "FAILED"])
def test_missing_answer_prevents_quality_scores_even_with_complete_context(reviews, agent_status):
    client, factory, ids = reviews
    assert client.patch(f"/api/v1/datasets/{ids['inputs'][0]}/cases/A", json={
        "required_metrics": ["correctness", "faithfulness"],
    }).status_code == 200
    run = freeze(client, ids["inputs"])
    execution = ExecutionResult(answer=None, agent_status=agent_status, started_at=now(), ended_at=now(),
        duration_ms=1, source_type="MOCK", context_scope="ACTUAL_MODEL_CONTEXT", context_completeness="COMPLETE",
        contexts=[Context(text="保存了上下文，但没有获得实际回答")])
    case_id, score_id = record(factory, run, result=execution)
    scope_ids = {**ids, "run": run, "case": case_id, "score": score_id}
    before = client.get(f"/api/v1/case-runs/{case_id}/scoring", params={"score_set_id": score_id}).json()
    view = client.get(f"/api/v1/extensions/reviews/cases/{case_id}", params={"score_set_id": score_id}).json()
    assert view["applicable_metrics"] == []
    for metric in ("correctness", "faithfulness"):
        response = client.post("/api/v1/extensions/reviews", json=review_input(scope_ids, scores={metric: 5}))
        assert response.status_code == 422 and response.json()["code"] == "REVIEW_METRIC_NOT_APPLICABLE"
    assert client.post("/api/v1/extensions/reviews", json=review_input(scope_ids, conclusion="UNDETERMINED", scores={})).status_code == 201
    assert client.get(f"/api/v1/case-runs/{case_id}/scoring", params={"score_set_id": score_id}).json() == before


def test_review_scope_isolated_in_api_and_database(reviews):
    client, factory, ids = reviews
    other_run = freeze(client, ids["inputs"])
    _, other_score = record(factory, other_run)
    result = client.post("/api/v1/extensions/reviews", json=review_input(ids, score_set_id=other_score))
    assert result.status_code == 404
    assert client.get(f"/api/v1/extensions/reviews/cases/{ids['case']}", params={"score_set_id": other_score}).status_code == 404
    assert client.get("/api/v1/extensions/reviews", params={"run_id": ids["run"], "score_set_id": other_score}).status_code == 404
    with factory() as session:
        session.add(HumanReview(run_id=ids["run"], case_run_id=ids["case"], score_set_id=other_score,
                                reviewer_name="合成", conclusion="FAIL", scores={}, reason="合成", human_confirmed=True))
        with pytest.raises(IntegrityError):
            session.flush()


def test_consistency_latest_only_valid_pairs_and_unknowns_not_zero(reviews):
    client, _, ids = reviews
    empty = client.get("/api/v1/extensions/reviews/consistency", params={"run_id": ids["run"]}).json()
    assert empty["conclusions"]["agreement_rate"] is None
    assert all(item["mean_absolute_difference"] is None for item in empty["metrics"])
    client.post("/api/v1/extensions/reviews", json=review_input(ids, scores={"correctness": 2}))
    client.post("/api/v1/extensions/reviews", json=review_input(ids, conclusion="PASS", scores={"correctness": 5}))
    client.post("/api/v1/extensions/reviews", json=review_input(ids, case_run_id=ids["second"], conclusion="UNDETERMINED", scores={"correctness": 4}))
    data = client.get("/api/v1/extensions/reviews/consistency", params={"run_id": ids["run"]}).json()
    assert data["review_count"] == 3 and data["latest_review_count"] == 2
    assert data["conclusions"]["valid_pair_count"] == 1
    assert data["conclusions"]["agreement_count"] == 0
    correctness = next(item for item in data["metrics"] if item["metric"] == "correctness")
    assert correctness["valid_pair_count"] == 1 and correctness["mean_absolute_difference"] == 3
    assert correctness["exact_agreement_count"] == 0
    assert correctness["disagreements"][0]["case_run_id"] == ids["case"]
    relevance = next(item for item in data["metrics"] if item["metric"] == "relevance")
    assert relevance["valid_pair_count"] == 0 and relevance["exact_agreement_rate"] is None


def test_consistency_rejects_invalid_persisted_judge_number_without_losing_other_metric(reviews):
    client, factory, ids = reviews
    client.post("/api/v1/extensions/reviews", json=review_input(ids, scores={"correctness": 2, "relevance": 2}))
    with factory.begin() as session:
        row = session.scalar(select(EvaluationScore).where(EvaluationScore.case_run_id == ids["case"], EvaluationScore.metric == "correctness"))
        row.score = 2.5
    data = client.get("/api/v1/extensions/reviews/consistency", params={"run_id": ids["run"]}).json()
    assert next(item for item in data["metrics"] if item["metric"] == "correctness")["valid_pair_count"] == 0
    assert next(item for item in data["metrics"] if item["metric"] == "relevance")["valid_pair_count"] == 1


def test_review_reasons_and_candidate_content_sanitized(reviews, monkeypatch):
    client, _, ids = reviews
    monkeypatch.setenv("HUMAN_REVIEW_SECRET", "review-secret-canary")
    result = client.post("/api/v1/extensions/reviews", json=review_input(ids, reason="review-secret-canary alice@example.com"))
    assert result.status_code == 201
    assert "review-secret-canary" not in result.text and "alice@example.com" not in result.text
    result = client.post("/api/v1/extensions/regression-drafts", json=draft_input(ids, reason="review-secret-canary"))
    assert result.status_code == 201
    assert "review-secret-canary" not in result.text


def passing_source(client, factory, ids):
    run = freeze(client, ids["inputs"])
    result = ExecutionResult(answer="断电后检查纸路", agent_status="SUCCEEDED", started_at=now(), ended_at=now(),
                             duration_ms=1200, source_type="MOCK", tools_completeness="COMPLETE",
                             tools=[ToolEvent(event_id="search-1", sequence=1, source="CHAT_TOOL", name="search", status="HIT")])
    case, score = record(factory, run, score=5, result=result)
    source = {**ids, "run": run, "case": case, "score": score}
    detail = client.get(f"/api/v1/case-runs/{case}/detail", params={"score_set_id": score}).json()
    assert detail["scoring"]["evaluation"]["conclusion"] == "PASS"
    assert detail["automatic_tags"] == detail["evaluation_issues"] == []
    assert detail["annotation"]["manual_tags"] == [] and detail["annotation"]["note"] == ""
    return source


def test_latest_human_failure_allows_draft_for_otherwise_passing_case(reviews):
    client, factory, ids = reviews
    source = passing_source(client, factory, ids)
    rejected = client.post("/api/v1/extensions/regression-drafts", json=draft_input(source))
    assert rejected.status_code == 422 and rejected.json()["code"] == "REGRESSION_SOURCE_NOT_BADCASE"
    before = client.get(f"/api/v1/case-runs/{source['case']}/scoring", params={"score_set_id": source["score"]}).json()
    review = client.post("/api/v1/extensions/reviews", json=review_input(source))
    assert review.status_code == 201, review.text
    created = client.post("/api/v1/extensions/regression-drafts", json=draft_input(source))
    assert created.status_code == 201, created.text
    assert created.json()["case_run_id"] == source["case"] and created.json()["score_set_id"] == source["score"]
    assert created.json()["case_snapshot"]["enabled"] is False
    assert client.get(f"/api/v1/case-runs/{source['case']}/scoring", params={"score_set_id": source["score"]}).json() == before


@pytest.mark.parametrize("latest_conclusion", ["PASS", "UNDETERMINED"])
def test_superseded_human_failure_does_not_qualify_passing_case(reviews, latest_conclusion):
    client, factory, ids = reviews
    source = passing_source(client, factory, ids)
    first = client.post("/api/v1/extensions/reviews", json=review_input(source))
    latest = client.post("/api/v1/extensions/reviews", json=review_input(source, conclusion=latest_conclusion))
    assert first.status_code == latest.status_code == 201, latest.text
    result = client.post("/api/v1/extensions/regression-drafts", json=draft_input(source))
    assert result.status_code == 422 and result.json()["code"] == "REGRESSION_SOURCE_NOT_BADCASE"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(RegressionDraft)) == 0


@pytest.mark.parametrize("other_scope", ["case", "score"])
def test_human_failure_in_other_case_or_score_does_not_qualify_source(reviews, other_scope):
    client, factory, ids = reviews
    source = passing_source(client, factory, ids)
    if other_scope == "case":
        other_case, _ = record(factory, source["run"], key="B", score=5)
        review_payload = review_input(source, case_run_id=other_case)
    else:
        with factory.begin() as session:
            original = session.get(ScoreSet, source["score"])
            other = ScoreSet(run_id=source["run"], parent_score_set_id=original.id, kind="RESCORE", status="COMPLETED",
                             judge_snapshot=original.judge_snapshot, rule_snapshot=original.rule_snapshot,
                             fingerprint=original.fingerprint)
            session.add(other)
            session.flush()
            other_score_id = other.id
        review_payload = review_input(source, score_set_id=other_score_id, scores={})
    review = client.post("/api/v1/extensions/reviews", json=review_payload)
    assert review.status_code == 201, review.text
    result = client.post("/api/v1/extensions/regression-drafts", json=draft_input(source))
    assert result.status_code == 422 and result.json()["code"] == "REGRESSION_SOURCE_NOT_BADCASE"


def test_draft_approval_disabled_idempotent_and_historical_source_protected(reviews):
    client, factory, ids = reviews
    before = client.get(f"/api/v1/case-runs/{ids['case']}/detail").json()
    result = client.post("/api/v1/extensions/regression-drafts", json=draft_input(ids))
    assert result.status_code == 201, result.text
    draft = result.json()
    assert draft["status"] == "PENDING" and draft["test_case_id"] is None
    assert draft["case_snapshot"]["question"] == before["case"]["case_snapshot"]["question"]
    assert draft["case_snapshot"]["expected_tools"] == ["search"]
    assert draft["case_snapshot"]["expected_answer"] != before["case"]["answer"]
    assert draft["case_snapshot"]["enabled"] is False
    approval = {"reviewer_name": "本机审批人", "human_confirmed": True}
    approved = client.post(f"/api/v1/extensions/regression-drafts/{draft['id']}/approve", json=approval)
    assert approved.status_code == 200, approved.text
    repeated = client.post(f"/api/v1/extensions/regression-drafts/{draft['id']}/approve", json={**approval, "reviewer_name": "另一个填报人"})
    assert repeated.json() == approved.json()
    assert approved.json()["status"] == "APPROVED" and approved.json()["approved_by"] == "本机审批人"
    case = client.get(f"/api/v1/datasets/{ids['target']}/cases").json()["items"][0]
    assert case["enabled"] is False and case["seen_regression"] is True
    assert case["source"]["regression_origin"]["case_run_id"] == ids["case"]
    assert case["source"]["regression_approval"]["identity_type"] == "SELF_DECLARED"
    assert client.get(f"/api/v1/case-runs/{ids['case']}/detail").json() == before
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(TestCase).where(TestCase.dataset_id == ids["target"])) == 1
        draft_row = session.get(RegressionDraft, draft["id"])
        with pytest.raises(TypeError):
            draft_row.case_snapshot["question"] = "篡改"


def test_draft_preserves_tool_assertions_and_preconditions(reviews):
    client, factory, ids = reviews
    created = client.post(f"/api/v1/datasets/{ids['inputs'][0]}/cases", json={
        "case_key": "TOOL", "question": "合成顺序与次数回归", "expected_answer": "先查后改",
        "expected_tools": ["lookup", "update"], "tool_match_mode": "exact",
        "tool_assertions": {"sequence": ["lookup", "update"], "max_calls": {"update": 1}},
        "required_metrics": ["tool_order", "tool_call_count"], "preconditions": {"prior_turns": ["合成前置"]},
    })
    assert created.status_code == 201, created.text
    run = freeze(client, ids["inputs"])
    with factory() as session:
        case = session.scalar(select(CaseRun).where(CaseRun.run_id == run, CaseRun.case_key == "TOOL"))
        score = client.get(f"/api/v1/runs/{run}/score-sets").json()["items"][0]["id"]
        case_id = case.id
    draft = client.post("/api/v1/extensions/regression-drafts", json=draft_input(ids, case_run_id=case_id, score_set_id=score)).json()
    result = client.post(f"/api/v1/extensions/regression-drafts/{draft['id']}/approve", json={"reviewer_name": "合成", "human_confirmed": True})
    assert result.status_code == 200, result.text
    case = client.get(f"/api/v1/datasets/{ids['target']}/cases").json()["items"][0]
    assert case["tool_assertions"]["sequence"] == ["lookup", "update"]
    assert case["tool_assertions"]["max_calls"] == {"update": 1}
    assert case["tool_match_mode"] == "exact" and case["expected_tools"] == ["lookup", "update"]
    assert case["preconditions"]["prior_turns"] == ["合成前置"]


def test_draft_conflict_does_not_overwrite_case_or_approve_draft(reviews):
    client, factory, ids = reviews
    existing = client.post(f"/api/v1/datasets/{ids['target']}/cases", json={"case_key": "REG-A", "question": "不能覆盖", "expected_answer": "原参考"}).json()
    draft = client.post("/api/v1/extensions/regression-drafts", json=draft_input(ids)).json()
    result = client.post(f"/api/v1/extensions/regression-drafts/{draft['id']}/approve", json={"reviewer_name": "合成", "human_confirmed": True})
    assert result.status_code == 409, result.text
    with factory() as session:
        assert session.get(RegressionDraft, draft["id"]).status == "PENDING"
        assert session.get(TestCase, existing["id"]).question == "不能覆盖"


@pytest.mark.parametrize("patch", [{"human_confirmed": False}, {"human_confirmed": 1}, {"reviewer_name": " "}])
def test_draft_approval_requires_explicit_human_confirmation(reviews, patch):
    client, _, ids = reviews
    draft = client.post("/api/v1/extensions/regression-drafts", json=draft_input(ids)).json()
    result = client.post(f"/api/v1/extensions/regression-drafts/{draft['id']}/approve", json={"reviewer_name": "合成", "human_confirmed": True, **patch})
    assert result.status_code == 422


def test_draft_scope_deleted_target_and_immutable_source(reviews):
    client, factory, ids = reviews
    other_run = freeze(client, ids["inputs"])
    _, other_score = record(factory, other_run)
    assert client.post("/api/v1/extensions/regression-drafts", json=draft_input(ids, score_set_id=other_score)).status_code == 404
    draft = client.post("/api/v1/extensions/regression-drafts", json=draft_input(ids)).json()
    with factory() as session:
        row = session.get(RegressionDraft, draft["id"])
        row.reason = "替换来源理由"
        with pytest.raises(ValueError, match="SOURCE_IMMUTABLE"):
            session.flush()
        session.rollback()
    assert client.delete(f"/api/v1/datasets/{ids['target']}").status_code == 204
    result = client.post(f"/api/v1/extensions/regression-drafts/{draft['id']}/approve", json={"reviewer_name": "合成", "human_confirmed": True})
    assert result.status_code == 404
    with factory() as session:
        assert session.get(RegressionDraft, draft["id"]).status == "PENDING"
