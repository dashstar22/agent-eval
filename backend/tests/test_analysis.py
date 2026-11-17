"""只读结果分析、离线证据和 CLI；夹具为 SQLite 合成事实，不调用模型。"""

import csv
from io import StringIO
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError
import pytest
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from agenteval.analysis import _condition_fingerprints, _csv_cell, export_report
from agenteval.analysis_schemas import RegressionPolicy, StabilityAnalysisInput
from agenteval.db import Base, make_engine, session_factory
from agenteval.evaluation_models import JudgeResult, JudgedScore
from agenteval.evaluation_store import load_summary_cases, save_assessment
from agenteval.main import create_app
from agenteval.models import ExecutionResult, ToolEvent, Usage, now
from agenteval.persistence import CaseRun, EvaluationRun, ScoreSet
from agenteval import regression_cli
from agenteval.rules import combine_metrics, evaluate_rules
from agenteval.schemas import RuleProfile
from agenteval.snapshots import digest
from test_workbench import freeze, metric, record, setup_inputs


@pytest.fixture
def analysis_db():
    engine = make_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    app = create_app(engine, runtime_enabled=False)
    app.state.session_factory = factory
    with TestClient(app) as client:
        yield client, factory
    engine.dispose()


def check(client, left, right, policy=None, **selections):
    body = {"baseline": {"run_id": left}, "candidate": {"run_id": right}}
    if policy is not None:
        body["policy"] = policy
    for side, score in selections.items():
        body[side]["score_set_id"] = score
    response = client.post("/api/v1/extensions/regression-checks", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def stability(client, *runs):
    response = client.post("/api/v1/extensions/stability-analysis", json={"selections": [
        item if isinstance(item, dict) else {"run_id": item} for item in runs]})
    assert response.status_code == 200, response.text
    return response.json()


def rescore(factory, run_id, original_id, *, score=2, status="COMPLETED", rule_changes=None):
    with factory.begin() as session:
        original = session.get(ScoreSet, original_id)
        rules = dict(original.rule_snapshot) | (rule_changes or {})
        row = ScoreSet(run_id=run_id, parent_score_set_id=original_id, kind="RESCORE",
                       judge_snapshot=dict(original.judge_snapshot), rule_snapshot=rules,
                       fingerprint=digest({"judge": dict(original.judge_snapshot), "rules": rules}), status=status)
        session.add(row)
        session.flush()
        profile = RuleProfile.model_validate(rules)
        judge = JudgeResult(status="SUCCEEDED", scores={name: JudgedScore(score=score, reason="重评分合成事实")
                            for name in ("correctness", "relevance", "task_completion")})
        cases = list(session.scalars(select(CaseRun).where(CaseRun.run_id == run_id)))
        source = {item.case_key: item for item in load_summary_cases(session, run_id, original_id)}
        for case in cases:
            execution = source[case.case_key].execution
            if execution:
                assessment = combine_metrics(dict(case.case_snapshot), profile,
                    evaluate_rules(dict(case.case_snapshot), execution, profile), judge)
                save_assessment(session, run_id, row.id, case.id, assessment)
        return row.id


def review(client, case_id, score_id, reason):
    response = client.post("/api/v1/extensions/reviews", json={"case_run_id": case_id,
        "score_set_id": score_id, "reviewer_name": "本地合成复核", "conclusion": "PASS",
        "scores": {"correctness": 5}, "reason": reason, "human_confirmed": True})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("policy", [
    {"max_new_failures": True}, {"max_new_failures": "1"}, {"max_new_failures": -1},
    {"min_coverage": float("nan")}, {"min_coverage": float("inf")}, {"min_coverage": 1.01},
    {"max_pass_rate_drop": -0.01}, {"max_pass_rate_drop": "0.2"}, {"unknown": 1},
])
def test_policy_strict_and_finite(policy):
    with pytest.raises(ValidationError):
        RegressionPolicy.model_validate(policy)


def test_analysis_request_scope_and_duplicate_execution_rejected(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    left, right = freeze(client, inputs), freeze(client, inputs)
    case_id, original = record(factory, left)
    selected = rescore(factory, left, original)
    for payload in [
        {"selections": [{"run_id": left}, {"run_id": left, "score_set_id": selected}]},
        {"selections": [{"run_id": "not-uuid"}, {"run_id": right}]},
        {"selections": [{"run_id": left}, {"run_id": right}], "execute": True},
        {"selections": [{"run_id": left}]},
    ]:
        assert client.post("/api/v1/extensions/stability-analysis", json=payload).status_code == 422
    assert client.post("/api/v1/extensions/regression-checks", json={
        "baseline": {"run_id": left}, "candidate": {"run_id": right, "score_set_id": original},
    }).status_code == 404
    with pytest.raises(ValidationError):
        StabilityAnalysisInput(selections=[{"run_id": left}] * 11)


def test_regression_gates_known_transitions_and_preserves_mock_scope(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=2)
    left, right = freeze(client, inputs), freeze(client, inputs)
    record(factory, left, "A", 5)
    record(factory, left, "B", 2)
    record(factory, right, "A", 2)
    record(factory, right, "B", 5)
    result = check(client, left, right)
    assert result["verdict"] == "FAIL"
    assert [item["case_key"] for item in result["new_failures"]] == ["A"]
    assert [item["case_key"] for item in result["fixed_cases"]] == ["B"]
    assert result["unknown_cases"] == []
    drop = next(item for item in result["checks"] if item["id"] == "pass_rate_drop")
    assert drop["actual"] == 0 and drop["status"] == "PASS"
    assert "case_run_id=" in result["new_failures"][0]["candidate_link"]
    assert result["conditions"]["source_scope"]["baseline"]["contains_simulation"] is True
    assert result["conditions"]["source_scope"]["candidate"]["real_quality_evidence"] is False
    permitted = check(client, left, right, {"max_new_failures": 1})
    assert permitted["verdict"] == "PASS"
    assert permitted["conditions"]["version_evidence"]["candidate"]["status"] == "INCOMPLETE"


def test_regression_pass_rate_drop_is_common_pairs_not_side_samples(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=3)
    left, right = freeze(client, inputs), freeze(client, inputs)
    record(factory, left, "A", 5)
    record(factory, right, "A", 2)
    record(factory, right, "B", 5)
    record(factory, right, "C", 5)
    result = check(client, left, right, {"min_coverage": 0.3, "max_new_failures": 1, "max_pass_rate_drop": 0.5})
    assert result["verdict"] == "FAIL"
    drop = next(item for item in result["checks"] if item["id"] == "pass_rate_drop")
    assert drop["actual"] == 1 and drop["status"] == "FAIL"
    assert len(result["unknown_cases"]) == 2
    assert metric(result["comparison"], "pass_rate")["paired_count"] == 1


def test_regression_threshold_accepts_mathematically_equal_drop_without_float_false_failure(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=3)
    for index in range(3, 10):
        assert client.post(f"/api/v1/datasets/{inputs[0]}/cases", json={
            "case_key": f"C{index}", "question": "合成边界问题", "expected_answer": "合成参考行为",
        }).status_code == 201
    left, right = freeze(client, inputs), freeze(client, inputs)
    keys = ["A", "B", "C", *(f"C{index}" for index in range(3, 10))]
    for index, key in enumerate(keys):
        record(factory, left, key, 5 if index < 8 else 2)
        record(factory, right, key, 5 if index < 5 else 2)
    value = check(client, left, right, {"max_new_failures": 3, "max_pass_rate_drop": 0.3})
    assert value["verdict"] == "PASS"
    assert next(item for item in value["checks"] if item["id"] == "pass_rate_drop")["actual"] == pytest.approx(0.3)


def test_unknown_and_missing_never_pass_default_gate_or_become_new_failures(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client)
    left, right = freeze(client, inputs), freeze(client, inputs)
    record(factory, left, "A", 5)
    record(factory, right, "B", 2, judge_error=True)
    result = check(client, left, right, {"min_coverage": 0.0})
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["new_failures"] == [] and len(result["unknown_cases"]) == 2
    assert "NO_COMMON_VALID_CONCLUSIONS" in result["conditions"]["limitations"]
    assert metric(result["comparison"], "pass_rate")["delta"] is None


def test_coverage_uses_each_side_and_paired_denominator(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=3)
    left, right = freeze(client, inputs), freeze(client, inputs)
    record(factory, left, "A", 5)
    record(factory, left, "B", 5)
    record(factory, right, "B", 5)
    record(factory, right, "C", 5)
    result = check(client, left, right, {"min_coverage": 0.5})
    assert result["conditions"]["source_scope"]
    assert result["comparison"]["left"]["summary"]["determinate_coverage"] == 2 / 3
    assert result["comparison"]["right"]["summary"]["determinate_coverage"] == 2 / 3
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["conditions"]["coverage_sufficient"] is False


@pytest.mark.parametrize("change", [{"rule_profile": {"response_time_ms": 2000}}, {"judge_profile": {"model": "different"}}])
def test_mismatched_rule_or_judge_never_passes_regression(analysis_db, change):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    left, right = freeze(client, inputs), freeze(client, inputs, **change)
    record(factory, left)
    record(factory, right)
    result = check(client, left, right)
    assert result["verdict"] == "INCONCLUSIVE" and result["conditions"]["comparable"] is False
    assert next(item for item in result["checks"] if item["id"] == "new_failures")["actual"] is None


def test_regression_selected_score_and_run_both_must_be_complete(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    left, right = freeze(client, inputs), freeze(client, inputs)
    record(factory, left)
    _, original = record(factory, right)
    new = rescore(factory, right, original, score=5, status="RUNNING")
    result = check(client, left, right, candidate=new)
    assert result["verdict"] == "INCONCLUSIVE" and result["conditions"]["completed"] is False
    with factory.begin() as session:
        session.get(ScoreSet, new).status = "COMPLETED"
        session.get(EvaluationRun, right).status = "RUNNING"
    assert check(client, left, right, candidate=new)["verdict"] == "INCONCLUSIVE"
    with factory.begin() as session:
        session.get(EvaluationRun, right).status = "COMPLETED"
    assert check(client, left, right, candidate=new)["verdict"] == "PASS"
    assert check(client, left, right, candidate=original)["verdict"] == "PASS"


def test_regression_changed_frozen_case_is_unknown_instead_of_fixed(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    baseline = freeze(client, inputs)
    record(factory, baseline, score=2)
    assert client.patch(f"/api/v1/datasets/{inputs[0]}/cases/A", json={"question": "修改后的评测问题"}).status_code == 200
    candidate = freeze(client, inputs)
    record(factory, candidate, score=5)
    value = check(client, baseline, candidate)
    assert value["verdict"] == "INCONCLUSIVE" and value["fixed_cases"] == []
    assert len(value["unknown_cases"]) == 1 and value["unknown_cases"][0]["content_match"] is False
    assert "DATA_SNAPSHOT_MISMATCH" in value["conditions"]["limitations"]


def test_stability_uses_actual_runs_pf_u_flips_and_metric_specific_samples(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=2)
    runs = [freeze(client, inputs, version_label=label) for label in ("first", "second", "third")]
    record(factory, runs[0], "A", 5)
    record(factory, runs[1], "A", 2)
    record(factory, runs[2], "A", 5)
    record(factory, runs[0], "B", 3, errored_metric="correctness")
    record(factory, runs[1], "B", 4, errored_metric="relevance")
    result = stability(client, *runs)
    assert result["comparable"] is True and result["run_count"] == 3
    a, b = result["cases"]
    assert (a["P"], a["F"], a["U"], a["flip_count"], a["flipped"]) == (2, 1, 0, 2, True)
    # 单指标 ERROR 不吞掉其他必评指标已知 FAIL；另外两次仍保持未判定。
    assert (b["P"], b["F"], b["U"], b["valid_count"], b["flip_count"]) == (0, 1, 2, 1, None)
    correctness, relevance = metric(result, "correctness"), metric(result, "relevance")
    assert correctness["valid_count"] == 4 and correctness["total_count"] == 6
    assert correctness["mean"] == 4 and correctness["stddev"] == pytest.approx((1 + 4 + 1) ** 0.5 / 2)
    assert relevance["mean"] == 3.75 and relevance["valid_count"] == 4
    assert metric(result, "total_tokens")["mean"] is None
    assert metric(result, "total_tokens")["valid_count"] == 0
    assert result["conditions"]["actual_versions_verified"] is False


def test_stability_skips_unknown_flips_but_preserves_unknown_count_and_singleton_null_stddev(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    runs = [freeze(client, inputs) for _ in range(3)]
    record(factory, runs[0], score=5)
    record(factory, runs[1], judge_error=True)
    result = ExecutionResult(answer="合成回答", agent_status="SUCCEEDED", started_at=now(), ended_at=now(), duration_ms=5,
        source_type="MOCK", usage=Usage(status="COMPLETE", input_tokens=2, output_tokens=3, total_tokens=5))
    record(factory, runs[2], score=2, result=result)
    value = stability(client, *runs)
    assert value["cases"][0]["P"] == value["cases"][0]["F"] == value["cases"][0]["U"] == 1
    assert value["cases"][0]["flip_count"] == 1
    tokens = metric(value, "total_tokens")
    assert (tokens["mean"], tokens["stddev"], tokens["valid_count"], tokens["total_count"]) == (5, None, 1, 3)
    assert tokens["reason"] == "SINGLE_VALID_OBSERVATION_NO_DISPERSION"


@pytest.mark.parametrize("change,reason", [
    ({"execution_budget": {"max_agent_calls": 2}}, "BUDGET_FINGERPRINT_MISMATCH"),
    ({"rule_profile": {"response_time_ms": 2000}}, "RULES_FINGERPRINT_MISMATCH"),
    ({"judge_profile": {"model": "new-judge"}}, "JUDGE_FINGERPRINT_MISMATCH"),
])
def test_stability_mismatched_frozen_conditions_preserve_facts_and_suppress_combined_stats(analysis_db, change, reason):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    left, right = freeze(client, inputs), freeze(client, inputs, **change)
    record(factory, left, score=5)
    record(factory, right, score=2)
    result = stability(client, left, right)
    assert result["comparable"] is False and reason in result["limitations"]
    assert result["cases"][0]["P"] == result["cases"][0]["F"] == 1
    assert result["cases"][0]["flipped"] is None and result["cases"][0]["flip_count"] is None
    assert metric(result, "correctness")["valid_count"] == 2
    assert all(item["mean"] is None and item["stddev"] is None and item["min"] is None and item["max"] is None for item in result["metrics"])


def test_stability_agent_case_and_dataset_changes_are_detected(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    left = freeze(client, inputs)
    record(factory, left)
    client.patch(f"/api/v1/datasets/{inputs[0]}/cases/A", json={"question": "新问题"})
    changed_case = freeze(client, inputs)
    record(factory, changed_case)
    value = stability(client, left, changed_case)
    assert {"CASES_FINGERPRINT_MISMATCH", "DATASET_FINGERPRINT_MISMATCH"} <= set(value["limitations"])
    client.patch(f"/api/v1/agent-configs/{inputs[1]}", json={"timeout_ms": 200000})
    changed_agent = freeze(client, inputs)
    record(factory, changed_agent)
    assert "AGENT_FINGERPRINT_MISMATCH" in stability(client, changed_case, changed_agent)["limitations"]


def test_stability_all_frozen_dimensions_include_revisions_and_components(analysis_db):
    client, factory = analysis_db
    run = freeze(client, setup_inputs(client, count=1))
    with factory() as session:
        saved = session.get(EvaluationRun, run)
        score = session.scalar(select(ScoreSet).where(ScoreSet.run_id == run))
        original = _condition_fingerprints(saved, score)
        class Snapshot:
            pass
        revised = Snapshot()
        revised.snapshot = json.loads(json.dumps(saved.snapshot))
        revised.snapshot["components"]["agenteval"] = "different-version"
        assert _condition_fingerprints(revised, score)["components"] != original["components"]
        revised.snapshot["cases"][0]["revision"] += 1
        assert _condition_fingerprints(revised, score)["cases"] != original["cases"]


def test_stability_tool_source_profile_and_selected_rescore_are_applied(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    runs = [freeze(client, inputs, rule_profile={"tool_sources": ["SERVER_COORDINATOR"]}) for _ in range(2)]
    selected = []
    for run in runs:
        execution = ExecutionResult(answer="合成回答", agent_status="SUCCEEDED", started_at=now(), ended_at=now(), duration_ms=7,
            source_type="MOCK", tools_completeness="COMPLETE", tools=[
                ToolEvent(event_id="chat", sequence=1, source="CHAT_TOOL", name="search", status="FAILED"),
                ToolEvent(event_id="server", sequence=2, source="SERVER_COORDINATOR", name="search", status="SUCCEEDED")])
        _, original = record(factory, run, score=5, result=execution)
        new = rescore(factory, run, original, score=2)
        selected.append({"run_id": run, "score_set_id": new})
    value = stability(client, *selected)
    assert value["comparable"] is True
    assert metric(value, "tool_success_rate")["mean"] == 1
    assert metric(value, "correctness")["mean"] == 2
    assert value["cases"][0]["F"] == 2
    assert all(item["kind"] == "RESCORE" for item in value["runs"])


def test_stability_incomplete_run_never_claims_stable(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    left, right = freeze(client, inputs), freeze(client, inputs)
    record(factory, left)
    result = stability(client, left, right)
    assert result["comparable"] is False and result["conditions"]["completed"] is False
    assert result["cases"][0]["U"] == 1
    assert metric(result, "correctness")["valid_count"] == 1 and metric(result, "correctness")["mean"] is None


def test_stability_includes_configured_tool_order_and_count_metrics(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    assert client.patch(f"/api/v1/datasets/{inputs[0]}/cases/A", json={
        "tool_assertions": {"sequence": ["search", "lookup"], "max_calls": {"search": 1}},
        "required_metrics": ["tool_order", "tool_call_count"],
    }).status_code == 200
    runs = [freeze(client, inputs) for _ in range(2)]
    for run in runs:
        execution = ExecutionResult(answer="合成回答", agent_status="SUCCEEDED", started_at=now(), ended_at=now(), duration_ms=7,
            source_type="MOCK", tools_completeness="COMPLETE", tools=[
                ToolEvent(event_id="1", sequence=1, source="CHAT_TOOL", name="lookup", status="SUCCEEDED"),
                ToolEvent(event_id="2", sequence=2, source="CHAT_TOOL", name="search", status="SUCCEEDED"),
                ToolEvent(event_id="3", sequence=3, source="CHAT_TOOL", name="search", status="SUCCEEDED")])
        record(factory, run, result=execution)
    value = stability(client, *runs)
    assert metric(value, "tool_order")["mean"] == metric(value, "tool_call_count")["mean"] == 0
    assert metric(value, "tool_call_count")["valid_count"] == 2 and value["cases"][0]["F"] == 2


def test_analysis_and_all_exports_leave_persisted_records_unchanged(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    left, right = freeze(client, inputs), freeze(client, inputs)
    record(factory, left, attempts=True)
    case_id, score_id = record(factory, right)
    review(client, case_id, score_id, "只读分析前的合成复核")
    def database_snapshot():
        with factory() as session:
            return {table.name: [tuple(row) for row in session.execute(select(table))]
                    for table in Base.metadata.tables.values()}
    before = database_snapshot()
    check(client, left, right)
    stability(client, left, right)
    for format in ("json", "csv", "html"):
        response = client.get(f"/api/v1/extensions/reports/{right}/export", params={"format": format})
        assert response.status_code == 200
    assert database_snapshot() == before


def test_export_selected_score_preserves_full_evidence_annotations_and_human_reviews(analysis_db, monkeypatch):
    client, factory = analysis_db
    monkeypatch.setenv("ANALYSIS_SECRET", "analysis-canary-private")
    inputs = setup_inputs(client, count=1)
    run = freeze(client, inputs)
    execution = ExecutionResult(answer="<script>alert(1)</script> analysis-canary-private alice@example.com", agent_status="SUCCEEDED",
        started_at=now(), ended_at=now(), duration_ms=42, source_type="MOCK", tools_completeness="COMPLETE",
        tools=[ToolEvent(event_id="proof", sequence=1, source="CHAT_TOOL", name="search", status="HIT",
                         result={"text": "完整合成工具证据", "Authorization": "Bearer another-canary"})])
    case_id, original = record(factory, run, score=5, result=execution, attempts=True)
    selected = rescore(factory, run, original, score=2)
    for score_id, note in [(original, "original-annotation"), (selected, "selected-annotation")]:
        assert client.put(f"/api/v1/case-runs/{case_id}/annotation", json={"score_set_id": score_id, "note": note}).status_code == 200
    old_review = review(client, case_id, original, "original-review")
    new_review = review(client, case_id, selected, "selected-review")
    url = f"/api/v1/extensions/reports/{run}/export"
    result = client.get(url, params={"score_set_id": selected, "format": "json"})
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("application/json")
    assert result.headers["cache-control"] == "no-store" and result.headers["x-content-type-options"] == "nosniff"
    assert selected in result.headers["content-disposition"] and "attachment" in result.headers["content-disposition"]
    data = result.json()
    item = data["cases"][0]
    assert data["report"]["score_set"]["id"] == selected and data["report"]["summary"]["F"] == 1
    assert item["annotation"]["note"] == "selected-annotation"
    assert item["human_reviews"][0]["id"] == new_review["id"] and item["latest_human_review"]["id"] == new_review["id"]
    assert old_review["id"] not in result.text and "original-review" not in result.text and "original-annotation" not in result.text
    assert "analysis-canary-private" not in result.text and "alice@example.com" not in result.text and "another-canary" not in result.text
    assert "完整合成工具证据" in result.text and item["traces"][0]["event_id"] == "proof"
    assert item["case"]["case_snapshot"]["question"] == "A如何排查卡纸？"
    original_doc = client.get(url, params={"score_set_id": original}).json()
    assert len(original_doc["cases"][0]["scoring"]["judge_attempts"]) == 2
    html = client.get(url, params={"score_set_id": selected, "format": "html"})
    assert "<script>" not in html.text and "&lt;script&gt;" in html.text
    assert 'default-src \'none\'' in html.text and "selected-review" in html.text
    assert "<script src=" not in html.text and '<link href="http' not in html.text
    csv_result = client.get(url, params={"score_set_id": selected, "format": "csv"})
    rows = list(csv.DictReader(StringIO(csv_result.content.decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["score_set_id"] == selected
    assert rows[0]["conclusion"] == "FAIL" and rows[0]["manual_note"] == "selected-annotation"
    assert json.loads(rows[0]["human_reviews"])[0]["id"] == new_review["id"]
    assert json.loads(rows[0]["latest_human_review"])["id"] == new_review["id"]
    assert client.get(url, params={"format": "xlsx"}).status_code == 422
    assert client.get(url, params={"score_set_id": "bad-id"}).status_code == 422
    other = freeze(client, inputs)
    _, other_score = record(factory, other)
    assert client.get(url, params={"score_set_id": other_score}).status_code == 404


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1)", "-1+2", "@cmd", "  =1", "\tplain", "\rvalue", "\nvalue", "\ufeff=1"])
def test_csv_formula_cells_are_prefixed(value):
    assert _csv_cell(value) == "'" + value


def test_export_csv_protects_actual_user_fields_and_preserves_unknowns(analysis_db):
    client, factory = analysis_db
    inputs = setup_inputs(client, count=1)
    assert client.patch(f"/api/v1/datasets/{inputs[0]}/cases/A", json={"question": "=HYPERLINK(\"bad\")"}).status_code == 200
    run = freeze(client, inputs)
    result = ExecutionResult(answer="\t@malicious", agent_status="SUCCEEDED", started_at=now(), ended_at=now(), duration_ms=1, source_type="MOCK")
    case_id, score_id = record(factory, run, result=result)
    client.put(f"/api/v1/case-runs/{case_id}/annotation", json={"score_set_id": score_id, "note": "+formula"})
    with factory() as session:
        value = export_report(session, run, score_id, "csv")
    row = list(csv.DictReader(StringIO(value["content"].decode("utf-8-sig"))))[0]
    assert row["question"].startswith("'=") and row["answer"].startswith("'\t@") and row["manual_note"] == "'+formula"
    assert "null" in row["usage"]


def mock_cli(monkeypatch, *, verdict="PASS", status=200, body=None, error=None):
    calls = []
    actual_client = httpx.Client
    def handler(request):
        calls.append(request)
        if error:
            raise error
        if body is not None:
            return httpx.Response(status, text=body)
        return httpx.Response(status, json={"verdict": verdict, "checks": [], "conditions": {"note": "合成保存结果"},
                                           "new_failures": [], "fixed_cases": [], "unknown_cases": []})
    def fake_client(**kwargs):
        assert kwargs == {"timeout": 60, "trust_env": False, "follow_redirects": False}
        return actual_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(regression_cli.httpx, "Client", fake_client)
    return calls


def cli_args(base="http://127.0.0.1:18080"):
    return ["--base-url", base, "--baseline", str(uuid4()), "--candidate", str(uuid4())]


@pytest.mark.parametrize("verdict,code", [("PASS", 0), ("FAIL", 1), ("INCONCLUSIVE", 2)])
def test_cli_exit_codes_and_analysis_only_request(monkeypatch, capsys, verdict, code):
    calls = mock_cli(monkeypatch, verdict=verdict)
    args = cli_args()
    assert regression_cli.main(args) == code
    assert json.loads(capsys.readouterr().out)["verdict"] == verdict
    assert len(calls) == 1 and calls[0].method == "POST" and calls[0].url.path == "/api/v1/extensions/regression-checks"
    payload = json.loads(calls[0].content)
    assert payload["baseline"]["run_id"] == args[3] and payload["policy"]["min_coverage"] == 1


def test_cli_markdown_output_and_exclusive_output_file(monkeypatch, tmp_path, capsys):
    calls = mock_cli(monkeypatch)
    output = tmp_path / "gate.md"
    args = cli_args("http://127.0.0.1:18080/api/v1") + ["--format", "markdown", "--output", str(output)]
    assert regression_cli.main(args) == 0
    before = output.read_text(encoding="utf-8")
    assert "AgentEval 回归检查" in before and "结论：PASS" in before
    assert calls[0].url.path == "/api/v1/extensions/regression-checks"
    assert regression_cli.main(args) == 3
    assert output.read_text(encoding="utf-8") == before
    assert json.loads(capsys.readouterr().err)["exit_code"] == 3


@pytest.mark.parametrize("base", ["file:///tmp", "http://user:secret@localhost", "http://localhost?token=bad", "http://localhost/#fragment", "http://localhost:bad", "http://localhost:99999", "http://localhost:0", "http://["])
def test_cli_invalid_base_never_requests(monkeypatch, capsys, base):
    calls = mock_cli(monkeypatch)
    assert regression_cli.main(cli_args(base)) == 3
    assert calls == [] and json.loads(capsys.readouterr().err)["exit_code"] == 3


@pytest.mark.parametrize("options", [{"status": 500}, {"status": 302}, {"body": "not JSON"}, {"verdict": "SUCCESS"}, {"error": httpx.InvalidURL("bad secret")}, {"error": httpx.ConnectError("secret")}, {"body": "null"}])
def test_cli_request_or_shape_errors_exit_three_without_leaking_details(monkeypatch, capsys, options):
    mock_cli(monkeypatch, **options)
    assert regression_cli.main(cli_args()) == 3
    value = capsys.readouterr()
    assert "secret" not in value.err and json.loads(value.err)["exit_code"] == 3 and not value.out


def test_cli_invalid_arguments_exit_three_and_help_zero(monkeypatch, capsys):
    calls = mock_cli(monkeypatch)
    assert regression_cli.main([]) == 3
    assert regression_cli.main(cli_args() + ["--min-coverage", "nan"]) == 3
    assert not calls
    with pytest.raises(SystemExit) as result:
        regression_cli.main(["--help"])
    assert result.value.code == 0
    capsys.readouterr()
