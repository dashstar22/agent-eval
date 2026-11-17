"""评分保存的归属、历史、逐次记录和调用方事务边界。"""

import json
import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agenteval.db import Base, make_engine
from agenteval.errors import ApiError
from agenteval.evaluation_models import CaseAssessment, JudgeAttemptRecord, MetricResult
from agenteval.evaluation_store import append_judge_attempt, load_summary_cases, save_assessment, save_execution
from agenteval.models import ExecutionResult, ToolEvent, Usage, now
from agenteval.persistence import (
    AgentConfig, AgentTrace, CaseEvaluation, CaseRun, Dataset, EvaluationScore, JudgeAttempt, ScoreSet, TestCase,
)
from agenteval.schemas import CaseCreate, FreezeRequest
from agenteval.snapshots import freeze_run


@pytest.fixture(params=["sqlite"] + (["mysql"] if os.environ.get("AGENTEVAL_TEST_DATABASE_URL") else []))
def store(request):
    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else os.environ["AGENTEVAL_TEST_DATABASE_URL"]
    engine = make_engine(url)
    if request.param == "sqlite":
        Base.metadata.create_all(engine)
    else:
        assert engine.url.database.startswith("agenteval_")
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(connection, expire_on_commit=False, join_transaction_mode="create_savepoint") as session:
            yield session
        transaction.rollback()
    engine.dispose()


def frozen(store, judge_ref=None, required_metrics=None):
    dataset = Dataset(name="评分存储验证")
    agent = AgentConfig(name="模拟连接", adapter_type="MOCK", base_url="http://127.0.0.1", source_type="MOCK")
    store.add_all([dataset, agent])
    store.flush()
    for key in ["A", "B"]:
        values = CaseCreate(case_key=key, question="打印机卡纸怎么办", expected_answer="先断电检查卡纸",
                            required_metrics=required_metrics or ["answer_nonempty"]).model_dump()
        store.add(TestCase(dataset_id=dataset.id, **values))
    store.flush()
    run = freeze_run(store, FreezeRequest(dataset_id=dataset.id, agent_config_id=agent.id,
                                         judge_profile={"credential_ref": judge_ref} if judge_ref else {}))
    score = store.scalar(select(ScoreSet).where(ScoreSet.run_id == run.id))
    case = store.scalar(select(CaseRun).where(CaseRun.run_id == run.id, CaseRun.case_key == "A"))
    return run, score, case


def execution(**kwargs):
    return ExecutionResult(agent_status="SUCCEEDED", answer="先断电检查卡纸", started_at=now(), ended_at=now(),
                           duration_ms=123, source_type="MOCK", **kwargs)


def assessment(status="PASS", conclusion="PASS", judge_status="SUCCEEDED"):
    return CaseAssessment(metrics=[MetricResult(metric="answer_nonempty", status=status, required=True,
                                                reason="确定性规则记录")],
                          conclusion=conclusion, judge_status=judge_status)


def attempt(number=1, status="SUCCEEDED", **kwargs):
    values = {"attempt": number, "status": status,
              "request": {"model": "judge"},
              "response": {"raw": {"text": "原始响应"}, "parsed": {"correctness": {"score": 4, "reason": "满足参考行为"}}},
              "duration_ms": 10}
    values.update(kwargs)
    return JudgeAttemptRecord(**values)


def test_execution_assessment_and_attempt_roundtrip_with_pending_case(store):
    run, score, case = frozen(store)
    result = execution(tools=[ToolEvent(event_id="e1", sequence=1, source="CHAT_TOOL", name="search", status="HIT", result={"text": "卡纸资料"})],
                       tools_completeness="COMPLETE", usage=Usage(status="PARTIAL", input_tokens=7, reason="OUTPUT_UNKNOWN"))
    save_execution(store, case.id, result)
    append_judge_attempt(store, run.id, score.id, case.id, attempt())
    save_assessment(store, run.id, score.id, case.id, assessment())
    store.expire_all()
    rows = load_summary_cases(store, run.id, score.id)
    assert [row.case_key for row in rows] == ["A", "B"]
    saved, pending = rows
    assert saved.execution == result
    assert saved.execution.usage.input_tokens == 7 and saved.execution.usage.output_tokens is None
    assert saved.assessment.conclusion == "PASS"
    assert saved.judge.scores["correctness"].score == 4
    assert len(saved.judge.attempts) == 1
    assert pending.execution is None and pending.assessment is None and pending.judge is None
    trace = store.scalar(select(AgentTrace).where(AgentTrace.case_run_id == case.id))
    assert trace.payload == {"result": {"text": "卡纸资料"}} and trace.sequence == 1


@pytest.mark.parametrize("function", ["attempt", "assessment", "summary"])
def test_all_scoring_store_paths_reject_cross_run_scope(store, function):
    run, score, case = frozen(store)
    other_run, other_score, other_case = frozen(store)
    with pytest.raises(ApiError) as rejected:
        if function == "attempt":
            append_judge_attempt(store, run.id, score.id, other_case.id, attempt())
        elif function == "assessment":
            save_assessment(store, run.id, other_score.id, case.id, assessment())
        else:
            load_summary_cases(store, run.id, other_score.id)
    assert rejected.value.code == "SCORING_RUN_MISMATCH"
    assert store.scalar(select(func.count()).select_from(JudgeAttempt).where(JudgeAttempt.run_id == run.id)) == 0
    assert store.scalar(select(func.count()).select_from(CaseEvaluation).where(CaseEvaluation.run_id == run.id)) == 0


def test_helpers_flush_without_committing_callers_transaction(store):
    run, score, case = frozen(store)
    nested = store.begin_nested()
    save_execution(store, case.id, execution())
    append_judge_attempt(store, run.id, score.id, case.id, attempt())
    save_assessment(store, run.id, score.id, case.id, assessment())
    nested.rollback()
    store.expire_all()
    assert case.answer is None
    assert store.scalar(select(func.count()).select_from(EvaluationScore).where(EvaluationScore.run_id == run.id)) == 0
    assert store.scalar(select(func.count()).select_from(JudgeAttempt).where(JudgeAttempt.run_id == run.id)) == 0


def test_attempt_append_sequence_failure_then_success_and_history_protection(store):
    run, score, case = frozen(store)
    result = execution()
    save_execution(store, case.id, result)
    with pytest.raises(ApiError) as rejected:
        append_judge_attempt(store, run.id, score.id, case.id, attempt(2))
    assert rejected.value.code == "JUDGE_ATTEMPT_CONFLICT"
    append_judge_attempt(store, run.id, score.id, case.id,
                         attempt(status="ERROR", response={"raw": "invalid JSON"}, error={"code": "INVALID_JSON"}))
    append_judge_attempt(store, run.id, score.id, case.id, attempt(2))
    save_assessment(store, run.id, score.id, case.id, assessment())
    for function in [lambda: save_execution(store, case.id, result),
                     lambda: save_assessment(store, run.id, score.id, case.id, assessment()),
                     lambda: append_judge_attempt(store, run.id, score.id, case.id, attempt(2))]:
        with pytest.raises(ApiError) as rejected:
            function()
        assert "PROTECTED" in rejected.value.code
    loaded = load_summary_cases(store, run.id, score.id)[0]
    assert len(loaded.judge.attempts) == 2 and loaded.judge.status == "SUCCEEDED"
    assert loaded.judge.attempts[0].error["code"] == "INVALID_JSON"


def test_execution_identity_and_saved_trace_cannot_be_removed(store):
    run, score, case = frozen(store)
    result = execution(tools=[ToolEvent(event_id="e1", sequence=1, source="CHAT_TOOL", name="search", status="HIT")],
                       tools_completeness="COMPLETE")
    save_execution(store, case.id, result)
    with pytest.raises(ApiError) as rejected:
        save_execution(store, case.id, execution())
    assert rejected.value.code == "EXECUTION_ID_MISMATCH"
    with pytest.raises(ApiError) as rejected:
        save_execution(store, case.id, result.model_copy(update={"tools": []}))
    assert rejected.value.code == "EXECUTION_TRACE_REMOVAL_FORBIDDEN"
    assert case.observations["tools"][0]["event_id"] == "e1"


def test_no_empty_required_metrics_or_forged_conclusion(store):
    run, score, case = frozen(store)
    invalid = [CaseAssessment(metrics=[], conclusion="PASS"),
               CaseAssessment(metrics=[MetricResult(metric="correctness", status="PASS", reason="optional")], conclusion="PASS"),
               assessment(status="FAIL", conclusion="PASS")]
    for value in invalid:
        with pytest.raises(ApiError):
            save_assessment(store, run.id, score.id, case.id, value)
    assert store.scalar(select(func.count()).select_from(CaseEvaluation).where(CaseEvaluation.run_id == run.id)) == 0


def test_execution_and_judge_diagnostics_are_redacted_with_arbitrary_reference(store, monkeypatch):
    monkeypatch.setenv("ORDINARY_JUDGE_REFERENCE", "secret-canary-persistence")
    run, score, case = frozen(store, judge_ref="ORDINARY_JUDGE_REFERENCE")
    result = execution(raw_agent={"secret-canary-persistence": "private value", "text": "secret-canary-persistence alice@example.com"})
    save_execution(store, case.id, result)
    append_judge_attempt(store, run.id, score.id, case.id,
                         attempt(request={"input": "secret-canary-persistence"},
                                 response={"raw": {"text": "Authorization: Bearer private-auth"},
                                           "parsed": {"correctness": {"score": 4, "reason": "secret-canary-persistence"}}}))
    save_assessment(store, run.id, score.id, case.id, assessment())
    store.expire_all()
    stored = json.dumps({"execution": case.raw_agent, "attempt": store.scalar(select(JudgeAttempt).where(JudgeAttempt.case_run_id == case.id)).response}, ensure_ascii=False)
    for secret in ["secret-canary-persistence", "alice@example.com", "private-auth"]:
        assert secret not in stored
    loaded = load_summary_cases(store, run.id, score.id)[0]
    assert loaded.judge.scores["correctness"].reason == "[REDACTED]"


def test_judge_error_remains_separate_from_agent_success(store):
    run, score, case = frozen(store)
    save_execution(store, case.id, execution())
    append_judge_attempt(store, run.id, score.id, case.id,
                         attempt(status="ERROR", response=None, error={"code": "JUDGE_AUTH_ERROR"}))
    save_assessment(store, run.id, score.id, case.id,
                    assessment(status="ERROR", conclusion="UNDETERMINED", judge_status="ERROR"))
    loaded = load_summary_cases(store, run.id, score.id)[0]
    assert loaded.execution.agent_status == "SUCCEEDED"
    assert loaded.assessment.conclusion == "UNDETERMINED" and loaded.judge.status == "ERROR"
    assert loaded.judge.attempts[0].usage.total_tokens is None


@pytest.mark.parametrize("wrapped,partial", [(True, False), (True, True)])
def test_judge_response_envelope_and_unrecoverable_partial(store, wrapped, partial):
    run, score, case = frozen(store)
    response = {"data": {"parsed": {"correctness": {"score": 4, "reason": "可复核"}}},
                "completeness": "PARTIAL" if partial else "COMPLETE", "reason": "SIZE_LIMIT" if partial else None}
    append_judge_attempt(store, run.id, score.id, case.id, attempt(response=response))
    loaded = load_summary_cases(store, run.id, score.id)[0]
    if partial:
        assert loaded.judge.status == "ERROR" and loaded.judge.error_code == "JUDGE_RESPONSE_PARTIAL"
        assert loaded.judge.scores == {}
    else:
        assert loaded.judge.status == "SUCCEEDED" and loaded.judge.scores["correctness"].score == 4


def test_assessment_cannot_remove_frozen_required_metrics_or_invent_names(store):
    from agenteval.schemas import DEFAULT_REQUIRED

    run, score, case = frozen(store, required_metrics=DEFAULT_REQUIRED)
    with pytest.raises(ApiError) as rejected:
        save_assessment(store, run.id, score.id, case.id, assessment())
    assert rejected.value.code == "ASSESSMENT_REQUIRED_METRICS_MISMATCH"
    invented = CaseAssessment(metrics=[MetricResult(metric="invented", status="PASS", required=True, reason="伪造指标")], conclusion="PASS")
    with pytest.raises(ApiError) as rejected:
        save_assessment(store, run.id, score.id, case.id, invented)
    assert rejected.value.code == "UNKNOWN_ASSESSMENT_METRIC"
