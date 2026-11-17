"""评分事实的事务内保存与还原；不发请求、不提交事务、不重写旧评分。"""

import json
from collections import defaultdict

from pydantic import ValidationError
from sqlalchemy import select

from .db import utcnow
from .errors import ApiError
from .evaluation_models import (
    CaseAssessment, JudgeAttemptRecord, JudgedScore, JudgeResult, MetricResult, SummaryCase,
)
from .models import ExecutionResult, Usage
from .persistence import (
    AgentTrace, CaseEvaluation, CaseRun, EvaluationRun, EvaluationScore, JudgeAttempt, ScoreSet,
)
from .privacy import bounded_raw, known_secrets, sanitize
from .schemas import DEFAULT_REQUIRED, METRICS

MAX_RECORD_BYTES = 2097152


def _case(session, identity):
    case = session.get(CaseRun, identity)
    if case is None:
        raise ApiError(404, "CASE_RUN_NOT_FOUND", "执行用例不存在")
    return case


def _scope(session, run_id, score_set_id, case_run_id=None):
    run = session.get(EvaluationRun, run_id)
    score_set = session.get(ScoreSet, score_set_id)
    case = _case(session, case_run_id) if case_run_id is not None else None
    if run is None or score_set is None:
        raise ApiError(404, "SCORING_RESOURCE_NOT_FOUND", "运行或评分集不存在")
    if score_set.run_id != run_id or (case is not None and case.run_id != run_id):
        raise ApiError(409, "SCORING_RUN_MISMATCH", "评分集和用例必须属于同一运行")
    return run, score_set, case


def _secrets(run, score_set=None):
    refs = dict(run.snapshot.get("agent", {}).get("credential_refs", {}))
    refs["judge_ref"] = (score_set.judge_snapshot if score_set else run.snapshot.get("judge", {})).get("credential_ref")
    return known_secrets(refs)


def _safe_document(value, secrets):
    if value is None:
        return None
    return sanitize(value, secrets)


def _bounded_document(value, secrets):
    if value is None:
        return None
    record = bounded_raw(value, secrets, max_bytes=MAX_RECORD_BYTES)
    return record["data"] if record["completeness"] == "COMPLETE" else record


def _has_assessment(session, case_run_id, score_set_id=None):
    stmt = select(CaseEvaluation.id).where(CaseEvaluation.case_run_id == case_run_id)
    if score_set_id is not None:
        stmt = stmt.where(CaseEvaluation.score_set_id == score_set_id)
    return session.scalar(stmt) is not None


def save_execution(session, case_run_id, result: ExecutionResult):
    """保存回答及归一事实。评分开始后不可替换，观测补充只允许同一执行身份。"""
    case = _case(session, case_run_id)
    run = session.get(EvaluationRun, case.run_id)
    if (_has_assessment(session, case.id)
            or session.scalar(select(JudgeAttempt.id).where(JudgeAttempt.case_run_id == case.id)) is not None
            or session.scalar(select(EvaluationScore.id).where(EvaluationScore.case_run_id == case.id)) is not None):
        raise ApiError(409, "EXECUTION_HISTORY_PROTECTED", "评分已经开始，不能覆盖执行事实")
    secrets = _secrets(run)
    normalized = sanitize(result.model_dump(mode="json"), secrets)
    # 超过保存上限的原文先有界化；归一结构不截断后冒充可恢复的完整对象。
    normalized["raw_agent"] = _bounded_document(normalized["raw_agent"], secrets)
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > MAX_RECORD_BYTES:
        raise ApiError(422, "EXECUTION_RECORD_TOO_LARGE", "归一执行证据超过保存上限，尚未保存")
    ExecutionResult.model_validate(normalized)
    previous = (case.raw_agent or {}).get("normalized_execution")
    if previous is not None and previous.get("execution_id") != normalized["execution_id"]:
        raise ApiError(409, "EXECUTION_ID_MISMATCH", "已有回答不能被另一次执行替换")
    existing = {row.event_id: row for row in session.scalars(select(AgentTrace).where(AgentTrace.case_run_id == case.id))}
    if not set(existing) <= {event["event_id"] for event in normalized["tools"] or []}:
        raise ApiError(409, "EXECUTION_TRACE_REMOVAL_FORBIDDEN", "不能删除已经保存的工具事件")
    case.agent_status = result.agent_status
    case.answer = normalized["answer"]
    case.raw_agent = {"normalized_execution": normalized, "completeness": "COMPLETE", "reason": None}
    case.trace_id = normalized["trace_ref"]
    case.observations = {key: normalized[key] for key in (
        "tools", "tools_completeness", "contexts", "context_scope", "context_completeness",
        "observation_status", "observation_reason", "observed_version",
    )}
    case.usage = normalized["usage"]
    case.timing = {key: normalized[key] for key in ("started_at", "ended_at", "duration_ms")}
    case.error = {"code": normalized["error_code"]} if normalized["error_code"] else None
    case.phase = "AGENT_SAVED"
    for event in normalized["tools"] or []:
        trace = existing.get(event["event_id"])
        if trace is None:
            trace = AgentTrace(case_run_id=case.id, event_id=event["event_id"])
            session.add(trace)
        for key in ("sequence", "source", "kind", "name", "status", "duration_ms", "completeness"):
            setattr(trace, key, event[key])
        trace.payload = {"result": event["result"]}
    session.flush()
    return case


def append_judge_attempt(session, run_id, score_set_id, case_run_id, attempt: JudgeAttemptRecord):
    run, score_set, case = _scope(session, run_id, score_set_id, case_run_id)
    if _has_assessment(session, case.id, score_set.id):
        raise ApiError(409, "SCORING_HISTORY_PROTECTED", "用例评分已保存，不能追加覆盖历史")
    previous = list(session.scalars(select(JudgeAttempt).where(
        JudgeAttempt.score_set_id == score_set.id, JudgeAttempt.case_run_id == case.id,
    ).order_by(JudgeAttempt.attempt)))
    if attempt.attempt != len(previous) + 1 or (previous and previous[-1].status == "SUCCEEDED"):
        raise ApiError(409, "JUDGE_ATTEMPT_CONFLICT", "评分尝试须顺序追加且成功后不能重试")
    secrets = _secrets(run, score_set)
    row = JudgeAttempt(
        run_id=run.id, score_set_id=score_set.id, case_run_id=case.id,
        attempt=attempt.attempt, status=attempt.status,
        request=_bounded_document(attempt.request, secrets),
        response=_bounded_document(attempt.response, secrets),
        usage=_safe_document(attempt.usage.model_dump(mode="json"), secrets),
        duration_ms=attempt.duration_ms, error=_safe_document(attempt.error, secrets),
    )
    session.add(row)
    session.flush()
    return row


def save_assessment(session, run_id, score_set_id, case_run_id, assessment: CaseAssessment):
    run, score_set, case = _scope(session, run_id, score_set_id, case_run_id)
    names = [metric.metric for metric in assessment.metrics]
    if not names or len(names) != len(set(names)) or not any(metric.required for metric in assessment.metrics):
        raise ApiError(422, "INVALID_ASSESSMENT_METRICS", "评分须包含无重复的指标及非空必评集合")
    if not set(names) <= METRICS:
        raise ApiError(422, "UNKNOWN_ASSESSMENT_METRIC", "评分包含未知指标")
    required = [metric for metric in assessment.metrics if metric.required]
    if {metric.metric for metric in required} != set(case.case_snapshot.get("required_metrics", DEFAULT_REQUIRED)):
        raise ApiError(422, "ASSESSMENT_REQUIRED_METRICS_MISMATCH", "评分必评集合必须等于冻结用例配置")
    expected_conclusion = ("FAIL" if any(metric.status == "FAIL" for metric in required) else
                           "UNDETERMINED" if any(metric.status in {"NA", "ERROR"} for metric in required) else "PASS")
    if assessment.conclusion != expected_conclusion:
        raise ApiError(422, "ASSESSMENT_CONCLUSION_MISMATCH", "结论必须按必评指标计算")
    if (_has_assessment(session, case.id, score_set.id)
            or session.scalar(select(EvaluationScore.id).where(
                EvaluationScore.score_set_id == score_set.id, EvaluationScore.case_run_id == case.id,
            )) is not None):
        raise ApiError(409, "SCORING_HISTORY_PROTECTED", "该评分集已有结果，不能覆盖历史")
    secrets = _secrets(run, score_set)
    row = CaseEvaluation(run_id=run.id, score_set_id=score_set.id, case_run_id=case.id,
                         status=assessment.judge_status, conclusion=assessment.conclusion, completed_at=utcnow())
    session.add(row)
    session.add_all(EvaluationScore(
        run_id=run.id, score_set_id=score_set.id, case_run_id=case.id,
        **sanitize(metric.model_dump(mode="json"), secrets),
    ) for metric in assessment.metrics)
    case.phase = "EVALUATED"
    session.flush()
    return row


def load_summary_cases(session, run_id, score_set_id) -> list[SummaryCase]:
    run, score_set, _ = _scope(session, run_id, score_set_id)
    cases = list(session.scalars(select(CaseRun).where(CaseRun.run_id == run.id).order_by(CaseRun.case_key)))
    evaluations = {row.case_run_id: row for row in session.scalars(select(CaseEvaluation).where(
        CaseEvaluation.run_id == run.id, CaseEvaluation.score_set_id == score_set.id,
    ))}
    scores, attempts = defaultdict(list), defaultdict(list)
    for row in session.scalars(select(EvaluationScore).where(
        EvaluationScore.run_id == run.id, EvaluationScore.score_set_id == score_set.id,
    ).order_by(EvaluationScore.metric)):
        scores[row.case_run_id].append(row)
    for row in session.scalars(select(JudgeAttempt).where(
        JudgeAttempt.run_id == run.id, JudgeAttempt.score_set_id == score_set.id,
    ).order_by(JudgeAttempt.attempt)):
        attempts[row.case_run_id].append(row)
    output = []
    secrets = _secrets(run, score_set)
    for case in cases:
        normalized = sanitize((case.raw_agent or {}).get("normalized_execution"), secrets)
        execution = ExecutionResult.model_validate(normalized) if normalized is not None else None
        evaluation = evaluations.get(case.id)
        assessment = None
        if evaluation is not None:
            assessment = CaseAssessment(
                metrics=[MetricResult(**sanitize({key: getattr(metric, key) for key in (
                    "metric", "status", "score", "required", "reason", "evidence",
                )}, secrets)) for metric in scores[case.id]],
                conclusion=evaluation.conclusion, judge_status=evaluation.status,
            )
        records = [JudgeAttemptRecord(
            attempt=row.attempt, status=row.status, request=sanitize(row.request or {}, secrets),
            response=sanitize(row.response, secrets), usage=Usage.model_validate(row.usage or {}),
            duration_ms=row.duration_ms or 0, error=sanitize(row.error, secrets),
        ) for row in attempts[case.id]]
        judge = None
        if records:
            last = records[-1]
            response = last.response or {}
            response_partial = response.get("completeness") == "PARTIAL"
            content = response.get("data", response)
            parsed = content.get("parsed", {}) if last.status == "SUCCEEDED" and isinstance(content, dict) and not response_partial else {}
            try:
                judged_scores = {name: JudgedScore.model_validate(value) for name, value in parsed.items()}
            except (ValidationError, AttributeError, TypeError):
                judged_scores = {}
            judge = JudgeResult(status="SUCCEEDED" if last.status == "SUCCEEDED" and judged_scores else "ERROR",
                                scores=judged_scores, attempts=records,
                                error_code=((last.error or {}).get("code") if last.status == "ERROR" else
                                            "JUDGE_RESPONSE_PARTIAL" if response_partial else
                                            "JUDGE_RESPONSE_UNRECOVERABLE" if not judged_scores else None))
        elif assessment is not None:
            judge = JudgeResult(status=assessment.judge_status)
        output.append(SummaryCase(case_key=case.case_key, execution=execution, assessment=assessment, judge=judge))
    return output
