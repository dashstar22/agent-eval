"""真实数据库投影与 HTTP 契约；所有执行/评分为明确的本地合成夹具。"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agenteval.comparisons import compare_runs
from agenteval.db import Base, make_engine, session_factory
from agenteval.evaluation_models import JudgeAttemptRecord, JudgeResult, JudgedScore, SummaryCase
from agenteval.evaluation_store import append_judge_attempt, save_assessment, save_execution
from agenteval.main import create_app
from agenteval.models import Context, ExecutionResult, ObservedVersion, ToolEvent, Usage, UsageAttempt, now
from agenteval.persistence import CaseRun, EvaluationRun, ScoreSet
from agenteval.rules import combine_metrics, evaluate_rules
from agenteval.schemas import RuleProfile
from agenteval.snapshots import digest
from agenteval.versions import compare_versions, verify_versions


@pytest.fixture(params=["sqlite"] + (["mysql"] if os.environ.get("AGENTEVAL_TEST_DATABASE_URL") else []))
def workbench(request):
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
    app = create_app(engine, runtime_enabled=False)
    app.state.session_factory = factory
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, factory
    if connection is not None:
        transaction.rollback()
        connection.close()
    engine.dispose()


def setup_inputs(client, count=2):
    dataset = client.post("/api/v1/datasets", json={"name": "工作台合成夹具"}).json()
    config = client.post("/api/v1/agent-configs", json={"name": "Mock投影测试", "adapter_type": "MOCK"}).json()
    for key in list("ABC")[:count]:
        response = client.post(f"/api/v1/datasets/{dataset['id']}/cases", json={
            "case_key": key, "question": key + "如何排查卡纸？", "expected_answer": "断电后检查纸路", "expected_tools": ["search"]})
        assert response.status_code == 201, response.text
    return dataset["id"], config["id"]


def freeze(client, inputs, **kwargs):
    response = client.post("/api/v1/frozen-runs", json={"dataset_id": inputs[0], "agent_config_id": inputs[1], **kwargs})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def record(factory, run_id, key="A", score=4, *, result=None, errored_metric=None, judge_error=False, attempts=False):
    result = result or ExecutionResult(answer="断电后检查纸路", agent_status="SUCCEEDED", started_at=now(), ended_at=now(),
                                      duration_ms=1200, source_type="MOCK", tools=[], tools_completeness="COMPLETE")
    with factory.begin() as session:
        case = session.scalar(select(CaseRun).where(CaseRun.run_id == run_id, CaseRun.case_key == key))
        score_set = session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id, ScoreSet.kind == "ORIGINAL"))
        save_execution(session, case.id, result)
        profile = RuleProfile.model_validate(dict(score_set.rule_snapshot))
        judge = JudgeResult(status="ERROR", error_code="SYNTHETIC_JUDGE_ERROR") if judge_error else JudgeResult(status="SUCCEEDED", scores={name: JudgedScore(score=score, reason="合成评分夹具") for name in ("correctness", "relevance", "task_completion")})
        if attempts:
            append_judge_attempt(session, run_id, score_set.id, case.id, JudgeAttemptRecord(
                attempt=1, status="ERROR", request={"source_type": "PROTOCOL_DOUBLE"}, response={"text": "非法JSON夹具"}, error={"code": "INVALID_JSON"}))
            append_judge_attempt(session, run_id, score_set.id, case.id, JudgeAttemptRecord(
                attempt=2, status="SUCCEEDED", request={"source_type": "PROTOCOL_DOUBLE"},
                response={"parsed": {name: item.model_dump() for name, item in judge.scores.items()}}))
        assessment = combine_metrics(dict(case.case_snapshot), profile, evaluate_rules(dict(case.case_snapshot), result, profile), judge)
        if errored_metric:
            for item in assessment.metrics:
                if item.metric == errored_metric:
                    item.status, item.score, item.reason = "ERROR", None, "SYNTHETIC_PARTIAL_METRIC_ERROR"
            assessment.conclusion = "FAIL" if any(item.required and item.status == "FAIL" for item in assessment.metrics) else "UNDETERMINED"
        save_assessment(session, run_id, score_set.id, case.id, assessment)
        session.get(EvaluationRun, run_id).status = "COMPLETED"
        score_set.status = "COMPLETED"
        return case.id, score_set.id


def metric(data, name):
    return next(item for item in data["metrics"] if item["metric"] == name)


def test_comparison_uses_same_valid_pairs_and_keeps_other_valid_metric(workbench):
    client, factory = workbench
    inputs = setup_inputs(client)
    left, right = freeze(client, inputs), freeze(client, inputs)
    record(factory, left, "A", 5, errored_metric="relevance")
    record(factory, left, "B", 2)
    record(factory, right, "A", 3)
    record(factory, right, "B", 4, errored_metric="correctness")
    response = client.get("/api/v1/comparisons", params={"left_run_id": left, "right_run_id": right})
    assert response.status_code == 200, response.text
    data = response.json()
    correctness = metric(data, "correctness")
    assert correctness["paired_count"] == 1
    assert (correctness["left_mean"], correctness["right_mean"], correctness["delta"]) == (5, 3, -2)
    assert correctness["unpaired_reasons"] == {"RIGHT_METRIC_ERROR": 1}
    assert correctness["pairs"][0]["case_key"] == "A"
    assert data["cases"][0]["left_conclusion"] == "UNDETERMINED"
    assert metric(data, "faithfulness")["delta"] is None
    assert not data["version_comparison"]["verified_change"]


@pytest.mark.parametrize("change,limitation", [({"rule_profile": {"response_time_ms": 1000}}, "RULE_FINGERPRINT_MISMATCH"),
                                               ({"judge_profile": {"model": "other-judge"}}, "JUDGE_FINGERPRINT_MISMATCH")])
def test_different_scoring_conditions_only_side_by_side(workbench, change, limitation):
    client, factory = workbench
    inputs = setup_inputs(client)
    left, right = freeze(client, inputs), freeze(client, inputs, **change)
    record(factory, left, score=3)
    record(factory, right, score=5)
    with factory() as session:
        data = compare_runs(session, left, right)
    assert data["comparable"] is False and limitation in data["limitations"]
    assert metric(data, "correctness")["left_mean"] == 3
    assert metric(data, "correctness")["right_mean"] == 5
    assert all(item["delta"] is None for item in data["metrics"])


def test_changed_frozen_data_refuses_unconditional_delta_and_protects_old_question(workbench):
    client, factory = workbench
    inputs = setup_inputs(client)
    left = freeze(client, inputs)
    record(factory, left)
    client.patch(f"/api/v1/datasets/{inputs[0]}/cases/A", json={"question": "修改后问题"})
    right = freeze(client, inputs)
    record(factory, right)
    data = client.get("/api/v1/comparisons", params={"left_run_id": left, "right_run_id": right}).json()
    assert data["comparable"] is False and "DATA_SNAPSHOT_MISMATCH" in data["limitations"]
    assert not data["cases"][0]["content_match"]
    assert metric(data, "correctness")["paired_count"] == 0
    rows = client.get("/api/v1/evaluations", params={"run_id": left}).json()["items"]
    assert rows[0]["question"] == "A如何排查卡纸？"


def test_incomparable_means_are_explicit_side_samples_without_delta(workbench):
    client, factory = workbench
    inputs = setup_inputs(client)
    left = freeze(client, inputs)
    right = freeze(client, inputs, judge_profile={"model": "different-judge"})
    left_id, _ = record(factory, left, "A", score=5)
    right_id, _ = record(factory, right, "B", score=2)
    data = client.get("/api/v1/comparisons", params={"left_run_id": left, "right_run_id": right}).json()
    value = metric(data, "correctness")
    assert value["paired_count"] == 0 and value["paired_left_mean"] is None and value["paired_right_mean"] is None
    assert value["left_mean"] == 5 and value["right_mean"] == 2 and value["delta"] is None
    assert value["mean_scope"] == "SIDE_VALID_SAMPLES_ONLY"
    assert value["left_valid_count"] == 1 and value["right_valid_count"] == 1
    assert value["left_case_run_ids"] == [left_id] and value["right_case_run_ids"] == [right_id]
    report = client.get(f"/api/v1/reports/{left}", params={"compare_run_id": right}).json()
    point = next(item for item in report["charts"]["experiment_comparison"]["data"] if item["name"] == "correctness")
    assert point["case_run_ids"] == [left_id] and point["right_case_run_ids"] == [right_id]
    assert client.get("/api/v1" + point["link"]).json()["items"][0]["id"] == left_id
    assert client.get("/api/v1" + point["right_link"]).json()["items"][0]["id"] == right_id
    assert report["charts"]["metric_changes"]["available"] is False


def test_comparable_empty_common_sample_does_not_subtract_different_sets(workbench):
    client, factory = workbench
    inputs = setup_inputs(client)
    left, right = freeze(client, inputs), freeze(client, inputs)
    record(factory, left, "A", score=5)
    record(factory, right, "B", score=2)
    data = client.get("/api/v1/comparisons", params={"left_run_id": left, "right_run_id": right}).json()
    value = metric(data, "correctness")
    assert data["comparable"] is True and value["paired_count"] == 0
    assert value["left_mean"] is None and value["right_mean"] is None and value["delta"] is None
    assert value["left_available_count"] == 1 and value["right_available_count"] == 1
    assert value["mean_scope"] == "COMMON_VALID_PAIRS"


def test_detail_tool_order_result_and_context_are_same_frozen_execution(workbench):
    client, factory = workbench
    run = freeze(client, setup_inputs(client))
    result = ExecutionResult(answer="合成回答", agent_status="SUCCEEDED", started_at=now(), ended_at=now(), duration_ms=17,
        source_type="PROTOCOL_DOUBLE", tools=[ToolEvent(event_id="e1", sequence=1, source="CHAT_TOOL", name="search", status="HIT", result={"text": "实际合成工具结果"}),
            ToolEvent(event_id="e2", sequence=2, source="SERVER_COORDINATOR", name="check", status="PAGE_CONFIRMATION_REQUIRED")],
        tools_completeness="COMPLETE", contexts=[Context(text="引用片段", version="demo-v1")], context_scope="CITATION_SUBSET", context_completeness="PARTIAL",
        usage=Usage(status="PARTIAL", input_tokens=9))
    case_id, score_id = record(factory, run, result=result, attempts=True)
    data = client.get(f"/api/v1/case-runs/{case_id}/detail", params={"score_set_id": score_id}).json()
    assert [item["sequence"] for item in data["traces"]] == [1, 2]
    assert data["traces"][0]["payload"]["result"]["text"] == "实际合成工具结果"
    assert data["case"]["observations"]["context_scope"] == "CITATION_SUBSET"
    assert data["case"]["observations"]["context_completeness"] == "PARTIAL"
    assert data["case"]["usage"]["output_tokens"] is None
    assert data["scoring"]["score_set_id"] == score_id
    assert [item["attempt"] for item in data["scoring"]["judge_attempts"]] == [1, 2]
    assert data["scoring"]["judge_attempts"][0]["error"]["code"] == "INVALID_JSON"
    assert data["scoring"]["judge_attempts"][1]["response"]["parsed"]["correctness"]["reason"] == "合成评分夹具"
    assert data["case"]["raw_agent"]["normalized_execution"]["source_type"] == "PROTOCOL_DOUBLE"


def test_badcase_multiple_labels_judge_issue_and_manual_note_survive_reload(workbench, monkeypatch):
    client, factory = workbench
    inputs = setup_inputs(client)
    run = freeze(client, inputs)
    result = ExecutionResult(answer="错误回答", agent_status="SUCCEEDED", started_at=now(), ended_at=now(), duration_ms=5,
        source_type="MOCK", tools=[ToolEvent(event_id="e1", sequence=1, source="CHAT_TOOL", name="other", status="TOOL_TIMEOUT")], tools_completeness="COMPLETE")
    case_id, score_id = record(factory, run, score=2, result=result)
    second_id, _ = record(factory, run, key="B", judge_error=True)
    data = client.get("/api/v1/badcases", params={"run_id": run, "category": "ANSWER_INCORRECT"}).json()
    assert data["total"] == 1
    assert {"ANSWER_INCORRECT", "ANSWER_IRRELEVANT", "TASK_INCOMPLETE", "EXPECTED_TOOL_MISSING", "TOOL_FAILED"} <= set(data["items"][0]["automatic_tags"])
    issue_data = client.get("/api/v1/badcases", params={"run_id": run, "issue": "JUDGE_ERROR"}).json()
    assert issue_data["total"] == 1 and issue_data["items"][0]["id"] == second_id
    assert not set(issue_data["items"][0]["automatic_tags"]) & {"ANSWER_INCORRECT", "ANSWER_IRRELEVANT", "TASK_INCOMPLETE"}
    monkeypatch.setenv("WORKBENCH_TEST_SECRET", "secret-note-canary")
    response = client.put(f"/api/v1/case-runs/{case_id}/annotation", json={"score_set_id": score_id, "manual_tags": ["参考行为待复核"], "note": "人工补充 secret-note-canary alice@example.com"})
    assert response.status_code == 200, response.text
    assert "secret-note-canary" not in response.text and "alice@example.com" not in response.text
    refreshed = client.get(f"/api/v1/case-runs/{case_id}/detail").json()
    assert refreshed["annotation"]["manual_tags"] == ["参考行为待复核"]
    assert "人工补充" in refreshed["annotation"]["note"]
    assert refreshed["scoring"]["evaluation"]["conclusion"] == "FAIL"
    assert any(item["reason"] == "合成评分夹具" for item in refreshed["scoring"]["metrics"])
    assert client.get("/api/v1/badcases", params={"manual_tag": "参考行为待复核"}).json()["total"] == 1
    assert client.get("/api/v1/badcases", params={"run_id": run, "category": "ANSWER_INCORRECT,EXECUTION_TIMEOUT"}).json()["total"] == 1
    assert client.get("/api/v1/badcases", params={"category": "ANSWER_INCORRECT,unknown"}).status_code == 422


def test_annotation_scope_uuid_and_payload_validation(workbench):
    client, factory = workbench
    inputs = setup_inputs(client)
    first, second = freeze(client, inputs), freeze(client, inputs)
    case_id, _ = record(factory, first)
    _, wrong_score_id = record(factory, second)
    assert client.put(f"/api/v1/case-runs/{case_id}/annotation", json={"score_set_id": wrong_score_id}).status_code == 404
    assert client.get("/api/v1/reports/invalid").status_code == 422
    assert client.get(f"/api/v1/case-runs/{case_id}/detail", params={"score_set_id": wrong_score_id}).status_code == 404
    assert client.put(f"/api/v1/case-runs/{case_id}/annotation", json={"score_set_id": "invalid"}).status_code == 422
    assert client.get("/api/v1/evaluations", params={"metric": "unknown"}).status_code == 422
    assert client.get("/api/v1/badcases", params={"category": "unknown"}).status_code == 422
    assert client.get("/api/v1/evaluations", params={"case_run_ids": "invalid"}).status_code == 422


def test_optional_identifiers_reject_empty_string_and_cross_run_scope(workbench):
    client, factory = workbench
    inputs = setup_inputs(client)
    left, right = freeze(client, inputs), freeze(client, inputs)
    left_case, left_score = record(factory, left)
    _, right_score = record(factory, right)
    for path, params in [
        ("/api/v1/evaluations", {"run_id": ""}),
        ("/api/v1/evaluations", {"score_set_id": ""}),
        ("/api/v1/evaluations", {"case_run_ids": ""}),
        ("/api/v1/evaluations", {"case_run_ids": left_case + ",invalid"}),
        ("/api/v1/badcases", {"run_id": ""}),
        (f"/api/v1/case-runs/{left_case}/detail", {"score_set_id": ""}),
        (f"/api/v1/reports/{left}", {"score_set_id": ""}),
        (f"/api/v1/reports/{left}", {"compare_run_id": ""}),
        (f"/api/v1/reports/{left}", {"compare_score_set_id": right_score}),
        ("/api/v1/comparisons", {"left_run_id": left, "right_run_id": right, "left_score_set_id": ""}),
    ]:
        response = client.get(path, params=params)
        assert response.status_code == 422, (path, params, response.text)
    for path, params in [
        ("/api/v1/evaluations", {"run_id": left, "score_set_id": right_score}),
        ("/api/v1/badcases", {"run_id": left, "score_set_id": right_score}),
        (f"/api/v1/reports/{left}", {"compare_run_id": right, "compare_score_set_id": left_score}),
        ("/api/v1/comparisons", {"left_run_id": left, "right_run_id": right, "left_score_set_id": right_score}),
    ]:
        assert client.get(path, params=params).status_code == 404


def test_report_four_charts_real_membership_and_nulls(workbench):
    client, factory = workbench
    inputs = setup_inputs(client)
    left, right = freeze(client, inputs), freeze(client, inputs)
    case_id, _ = record(factory, left, score=2)
    record(factory, right, score=4)
    response = client.get(f"/api/v1/reports/{left}", params={"compare_run_id": right})
    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["summary"]["N"], data["summary"]["F"], data["summary"]["U"]) == (2, 1, 1)
    assert data["summary"]["usage"]["Agent"]["total_tokens"] is None
    assert data["summary"]["usage"]["Agent"]["cost"] is None
    assert set(data["charts"]) == {"experiment_comparison", "metric_changes", "badcase_categories", "latency_distribution"}
    duration_row = data["charts"]["latency_distribution"]["data"][0]
    assert duration_row["case_run_ids"] == [case_id]
    assert client.get("/api/v1" + duration_row["link"]).json()["total"] == 1
    assert next(row for row in data["charts"]["metric_changes"]["data"] if row["name"] == "faithfulness")["value"] is None
    default = client.get(f"/api/v1/reports/{left}").json()
    assert default["charts"]["experiment_comparison"]["available"] is False
    assert default["charts"]["experiment_comparison"]["reason"] == "SELECT_SECOND_RUN"


def test_rescore_report_uses_own_status_and_retains_original_annotation(workbench):
    client, factory = workbench
    run = freeze(client, setup_inputs(client))
    case_id, original = record(factory, run)
    client.put(f"/api/v1/case-runs/{case_id}/annotation", json={"score_set_id": original, "note": "原评分复核"})
    with factory.begin() as session:
        old = session.get(ScoreSet, original)
        rules = dict(old.rule_snapshot) | {"response_time_ms": 2000}
        score = ScoreSet(run_id=run, parent_score_set_id=old.id, kind="RESCORE", judge_snapshot=dict(old.judge_snapshot), rule_snapshot=rules,
                         fingerprint=digest({"judge": dict(old.judge_snapshot), "rules": rules}), status="RUNNING")
        session.add(score)
        session.flush()
        new_id = score.id
    data = client.get(f"/api/v1/reports/{run}", params={"score_set_id": new_id}).json()
    assert data["summary"]["provisional"] is True and data["summary"]["U"] == 2
    assert client.get(f"/api/v1/case-runs/{case_id}/detail", params={"score_set_id": new_id}).json()["annotation"]["note"] == ""
    assert client.get(f"/api/v1/case-runs/{case_id}/detail", params={"score_set_id": original}).json()["annotation"]["note"] == "原评分复核"


def test_dashboard_defaults_to_real_scope_and_discloses_simulation(workbench):
    client, factory = workbench
    run = freeze(client, setup_inputs(client))
    record(factory, run)
    data = client.get("/api/v1/dashboard").json()
    assert data["summary"] is None and data["selected_run_id"] is None
    assert data["recent_runs"][0]["source_type"] == "MOCK"
    selected = client.get("/api/v1/dashboard", params={"source_type": "MOCK"}).json()
    assert selected["selected_run_id"] == run and selected["summary"]["source_scope"]["contains_simulation"]
    assert selected["summary"]["source_scope"]["real_quality_evidence"] is False


def test_empty_dashboard_and_report_do_not_generate_chart_values(workbench):
    client, _ = workbench
    empty = client.get("/api/v1/dashboard").json()
    assert empty["recent_runs"] == [] and empty["summary"] is None
    run = freeze(client, setup_inputs(client))
    report = client.get(f"/api/v1/reports/{run}").json()
    assert all(not chart["available"] and chart["data"] == [] for chart in report["charts"].values())
    assert report["summary"]["agent"]["success_duration_mean_ms"] is None


def test_paired_usage_includes_prerequisites_and_excludes_unknown_attempts(workbench):
    client, factory = workbench
    inputs = setup_inputs(client, count=1)
    left, right = freeze(client, inputs), freeze(client, inputs)
    prerequisite = ExecutionResult(answer="前置", agent_status="SUCCEEDED", started_at=now(), ended_at=now(), duration_ms=1,
        source_type="MOCK", usage=Usage(status="PARTIAL", input_tokens=5))
    result = ExecutionResult(answer="目标", agent_status="SUCCEEDED", started_at=now(), ended_at=now(), duration_ms=1,
        source_type="MOCK", usage=Usage(status="PARTIAL", input_tokens=7), prerequisites=[prerequisite.model_dump()])
    record(factory, left, result=result)
    record(factory, right, result=result.model_copy(update={"usage": Usage(status="PARTIAL", input_tokens=10)}))
    data = client.get("/api/v1/comparisons", params={"left_run_id": left, "right_run_id": right}).json()
    assert metric(data, "input_tokens")["left_mean"] == 12
    assert metric(data, "input_tokens")["right_mean"] == 15
    assert metric(data, "input_tokens")["delta"] == 3
    assert metric(data, "output_tokens")["delta"] is None


def version_case(*, source="REAL_EXTERNAL", build="a", prompt="p", model="demo-model", knowledge="v1", evidence=True):
    build = digest(build)
    prompt = digest(prompt)
    execution = ExecutionResult(answer="合成版本证据", agent_status="SUCCEEDED", started_at=now(), ended_at=now(), duration_ms=1,
        source_type=source, model_execution_verified=source == "REAL_EXTERNAL", context_scope="ACTUAL_MODEL_CONTEXT", context_completeness="COMPLETE",
        contexts=[Context(text="合成知识", version=knowledge)],
        observed_version=ObservedVersion(build_sha256=build, prompt_sha256=prompt, model_id=model, knowledge_versions=[knowledge], build_status="VERIFIED_PROCESS_JAR"),
        usage=Usage(status="UNAVAILABLE", attempts=[UsageAttempt(attempt=1, status="SUCCEEDED", source="MODEL_RESPONSE_METADATA", model_id=model, prompt_sha256=prompt)] if evidence else []))
    return SummaryCase(case_key="A", execution=execution)


def test_version_labels_do_not_prove_change_and_model_snapshot_stays_unknown():
    first = verify_versions({"candidate_version_label": "V1", "agent": {}}, [version_case()])
    second = verify_versions({"candidate_version_label": "V2", "agent": {}}, [version_case()])
    assert first["status"] == "VERIFIED" and first["model_snapshot_status"] == "UNKNOWN"
    assert not compare_versions(first, second)["verified_change"]
    changed = verify_versions({"agent": {}}, [version_case(prompt="p2")])
    assert compare_versions(first, changed)["verified_change"]


def test_version_expected_mismatch_is_visible_even_for_protocol_double():
    data = verify_versions({"agent": {"expected_version": {"prompt_sha256": "candidate"}}}, [version_case(source="PROTOCOL_DOUBLE")])
    dimension = next(row for row in data["dimensions"] if row["dimension"] == "prompt_sha256")
    assert dimension["status"] == "MISMATCH" and dimension["verification"] == "UNVERIFIED"
    assert dimension["expected_match"] is False
    assert data["status"] == "MISMATCH"


def test_model_verified_flag_alone_does_not_verify_prompt_or_model():
    data = verify_versions({"agent": {}}, [version_case(evidence=False)])
    dims = {item["dimension"]: item for item in data["dimensions"]}
    assert dims["prompt_sha256"]["status"] == "UNVERIFIED"
    assert dims["model_id"]["status"] == "UNVERIFIED"
    assert dims["build_sha256"]["status"] == "OBSERVED"


def test_unknown_model_placeholders_are_unavailable_even_with_a_claimed_flag():
    data = verify_versions({"agent": {}}, [version_case(model="UNKNOWN")])
    model = next(item for item in data["dimensions"] if item["dimension"] == "model_id")
    assert model["status"] == "UNKNOWN" and model["observed"] == []


def test_actual_knowledge_versions_cannot_be_hidden_by_a_subset_declaration():
    case = version_case(knowledge="v1")
    case.execution.contexts.append(Context(text="另一实际片段", version="v2"))
    data = verify_versions({"agent": {"expected_version": {"knowledge_version": "v1"}}}, [case])
    knowledge = next(item for item in data["dimensions"] if item["dimension"] == "knowledge_version")
    assert knowledge["observed"] == ["v1", "v2"]
    assert knowledge["status"] == "MISMATCH" and knowledge["expected_match"] is False
    assert knowledge["verification"] == "UNVERIFIED" and knowledge["verified_count"] == 0


@pytest.mark.parametrize("unknown", [None, "UNKNOWN", "UNAVAILABLE", ""])
def test_missing_actual_fragment_version_prevents_knowledge_verification(unknown):
    case = version_case(knowledge="v1")
    case.execution.contexts.append(Context(text="版本缺失片段", version=unknown))
    data = verify_versions({"agent": {"expected_version": {"knowledge_version": "v1"}}}, [case])
    knowledge = next(item for item in data["dimensions"] if item["dimension"] == "knowledge_version")
    assert knowledge["status"] == "UNVERIFIED" and knowledge["verification"] == "UNVERIFIED"
    assert knowledge["verified_count"] == 0


def test_complete_consistent_actual_knowledge_version_is_verified_only_for_real_evidence():
    case = version_case(knowledge="v1")
    case.execution.contexts.append(Context(text="相同版本第二片段", version="v1"))
    data = verify_versions({"agent": {"expected_version": {"knowledge_version": "v1"}}}, [case])
    knowledge = next(item for item in data["dimensions"] if item["dimension"] == "knowledge_version")
    assert knowledge["status"] == "MATCH" and knowledge["verification"] == "VERIFIED"
    simulated = version_case(source="PROTOCOL_DOUBLE", knowledge="v1")
    dimension = next(item for item in verify_versions({"agent": {}}, [simulated])["dimensions"] if item["dimension"] == "knowledge_version")
    assert dimension["verification"] == "UNVERIFIED"
