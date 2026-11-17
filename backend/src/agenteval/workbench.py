"""工作台持久数据投影；不调用执行器、适配器或 Judge。"""

from collections import Counter
from urllib.parse import urlencode

from sqlalchemy import select

from .comparisons import SCORE_FIELDS, compare_runs, comparison_side, resolve_score_set
from .db import utcnow
from .evaluation_store import load_summary_cases
from .errors import ApiError
from .persistence import AgentTrace, BadcaseAnnotation, CaseEvaluation, CaseRun, EvaluationRun, EvaluationScore, JudgeAttempt, ScoreSet
from .privacy import known_secrets, sanitize
from .scoring_api import _history, _score_set
from .services import case_run_view, public_row
from .schemas import METRICS, canonical_id
from .versions import verify_versions

AUTOMATIC_TAGS = ("ANSWER_INCORRECT", "ANSWER_IRRELEVANT", "TASK_INCOMPLETE", "EXPECTED_TOOL_MISSING", "TOOL_FAILED", "AGENT_ERROR", "EXECUTION_TIMEOUT")
QUALITY_TAGS = {"correctness": "ANSWER_INCORRECT", "relevance": "ANSWER_IRRELEVANT", "task_completion": "TASK_INCOMPLETE"}


def _safe(value, run=None, score_set=None):
    refs = dict(run.snapshot.get("agent", {}).get("credential_refs", {})) if run else {}
    if score_set:
        refs["judge_ref"] = score_set.judge_snapshot.get("credential_ref")
    return sanitize(value, known_secrets(refs))


def annotation_view(row):
    return public_row(row, "manual_tags note updated_at") if row else {"manual_tags": [], "note": "", "updated_at": None}


def labels(case, item):
    automatic, issues = set(), set()
    if case.agent_status == "TIMEOUT":
        automatic.add("EXECUTION_TIMEOUT")
    elif case.agent_status in {"FAILED", "PROTOCOL_ERROR"}:
        automatic.add("AGENT_ERROR")
    elif case.agent_status == "RESULT_UNKNOWN":
        issues.add("AGENT_RESULT_UNKNOWN")
    elif case.agent_status is None:
        issues.add("NOT_EXECUTED")
    if item.assessment:
        for metric in item.assessment.metrics:
            if metric.status == "FAIL":
                if metric.metric in QUALITY_TAGS:
                    automatic.add(QUALITY_TAGS[metric.metric])
                elif metric.metric == "expected_tools" and metric.reason == "EXPECTED_TOOLS_NOT_CALLED":
                    automatic.add("EXPECTED_TOOL_MISSING")
                elif metric.metric == "tool_success":
                    automatic.add("TOOL_FAILED")
                elif metric.metric == "answer_nonempty":
                    automatic.add("AGENT_ERROR")
            elif metric.required and metric.status in {"NA", "ERROR"}:
                issues.add("REQUIRED_METRIC_" + metric.status)
        if item.assessment.judge_status == "ERROR":
            issues.add("JUDGE_ERROR")
    else:
        issues.add("SCORING_PENDING")
    if item.judge and item.judge.status == "ERROR":
        issues.add("JUDGE_ERROR")
    execution = item.execution
    required = case.case_snapshot.get("required_metrics", [])
    if execution:
        if (case.case_snapshot.get("expected_tools") is not None or set(required) & {"expected_tools", "tool_names", "tool_success"}) and execution.tools_completeness != "COMPLETE":
            issues.add("TOOL_OBSERVATION_" + execution.tools_completeness)
        if "faithfulness" in required and (execution.context_scope != "ACTUAL_MODEL_CONTEXT" or execution.context_completeness != "COMPLETE"):
            issues.add("FULL_CONTEXT_UNAVAILABLE")
    return [tag for tag in AUTOMATIC_TAGS if tag in automatic], sorted(issues)


def run_projection(session, run, score_set):
    summaries = {item.case_key: item for item in load_summary_cases(session, run.id, score_set.id)}
    annotations = {row.case_run_id: row for row in session.scalars(select(BadcaseAnnotation).where(
        BadcaseAnnotation.run_id == run.id, BadcaseAnnotation.score_set_id == score_set.id))}
    output = []
    for case in session.scalars(select(CaseRun).where(CaseRun.run_id == run.id).order_by(CaseRun.case_key)):
        item = summaries[case.case_key]
        automatic, issues = labels(case, item)
        output.append({"id": case.id, "run_id": run.id, "score_set_id": score_set.id, "case_key": case.case_key,
                       "question": case.case_snapshot.get("question"), "agent_status": case.agent_status, "phase": case.phase,
                       "conclusion": item.assessment.conclusion if item.assessment else "UNDETERMINED",
                       "source_type": item.execution.source_type if item.execution else run.snapshot.get("agent", {}).get("source_type", "UNKNOWN"),
                       "model_execution_verified": bool(item.execution and item.execution.model_execution_verified),
                       "automatic_tags": automatic, "evaluation_issues": issues, "annotation": annotation_view(annotations.get(case.id)),
                       "metrics": [metric.model_dump() for metric in item.assessment.metrics] if item.assessment else [],
                       "duration_ms": item.execution.duration_ms if item.execution and item.execution.agent_status == "SUCCEEDED" else None,
                       "links": {"detail": "/evaluations?" + urlencode({"case_run_id": case.id, "run_id": run.id, "score_set_id": score_set.id}),
                                 "report": "/reports?" + urlencode({"run_id": run.id, "score_set_id": score_set.id})}})
    return _safe(output, run, score_set)


def evaluations(session, *, run_id=None, score_set_id=None, conclusion=None, metric=None, category=None,
                manual_tag=None, issue=None, case_run_ids=None, page=1, page_size=20, badcases_only=False):
    if metric is not None and metric not in METRICS:
        raise ApiError(422, "INVALID_FILTER", "指标筛选值不合法", [{"field": "metric", "code": "invalid"}])
    categories = set(category.split(",")) if category is not None else None
    if categories is not None and not categories <= set(AUTOMATIC_TAGS):
        raise ApiError(422, "INVALID_FILTER", "Badcase 分类不合法", [{"field": "category", "code": "invalid"}])
    if score_set_id is not None:
        selected = _history(session, ScoreSet, score_set_id)
        if run_id is None:
            run_id = selected.run_id
    runs = [_history(session, EvaluationRun, run_id)] if run_id is not None else list(session.scalars(select(EvaluationRun).order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())))
    selected_ids = set(case_run_ids.split(",")) if case_run_ids is not None else None
    if selected_ids is not None:
        try:
            for identity in selected_ids:
                canonical_id(identity)
        except ValueError:
            raise ApiError(422, "INVALID_FILTER", "用例筛选标识须为规范 UUID", [{"field": "case_run_ids", "code": "invalid"}]) from None
    output = []
    for run in runs:
        score_set = resolve_score_set(session, run.id, score_set_id)
        for row in run_projection(session, run, score_set):
            if badcases_only and not (row["automatic_tags"] or row["evaluation_issues"] or row["annotation"]["manual_tags"] or row["annotation"]["note"] or row["conclusion"] == "FAIL"):
                continue
            if conclusion and row["conclusion"] != conclusion:
                continue
            if metric and not any(item["metric"] == metric and item["status"] in {"PASS", "FAIL"} for item in row["metrics"]):
                continue
            if categories is not None and not categories.intersection(row["automatic_tags"]):
                continue
            if manual_tag and manual_tag not in row["annotation"]["manual_tags"]:
                continue
            if issue and issue not in row["evaluation_issues"]:
                continue
            if selected_ids is not None and row["id"] not in selected_ids:
                continue
            output.append(row)
    return {"items": output[(page - 1) * page_size:page * page_size], "total": len(output), "page": page, "page_size": page_size}


def detail(session, case_id, score_set_id=None):
    case = _history(session, CaseRun, case_id)
    run = _history(session, EvaluationRun, case.run_id)
    score = resolve_score_set(session, run.id, score_set_id)
    conditions = {"run_id": run.id, "score_set_id": score.id, "case_run_id": case.id}
    evaluation = session.scalar(select(CaseEvaluation).filter_by(**conditions))
    metrics = list(session.scalars(select(EvaluationScore).filter_by(**conditions).order_by(EvaluationScore.metric)))
    attempts = list(session.scalars(select(JudgeAttempt).filter_by(**conditions).order_by(JudgeAttempt.attempt)))
    sets = list(session.scalars(select(ScoreSet).where(ScoreSet.run_id == run.id).order_by(ScoreSet.created_at, ScoreSet.id)))
    traces = list(session.scalars(select(AgentTrace).where(AgentTrace.case_run_id == case.id).order_by(AgentTrace.sequence, AgentTrace.id)))
    annotation = session.scalar(select(BadcaseAnnotation).filter_by(**conditions))
    summaries = load_summary_cases(session, run.id, score.id)
    selected_summary = next(item for item in summaries if item.case_key == case.case_key)
    automatic, issues = labels(case, selected_summary)
    return _safe({"case": case_run_view(case), "score_set": public_row(score, SCORE_FIELDS),
                  "score_sets": [public_row(item, SCORE_FIELDS) for item in sets],
                  "scoring": {"run_id": run.id, "case_run_id": case.id, "score_set_id": score.id, "agent_status": case.agent_status,
                              "evaluation": public_row(evaluation, "status conclusion completed_at") if evaluation else None,
                              "metrics": [public_row(item, "metric status score required reason evidence") for item in metrics],
                              "judge_attempts": [public_row(item, "attempt status request response usage duration_ms error") for item in attempts]},
                  "traces": [public_row(item, "event_id sequence source kind name status duration_ms payload completeness") for item in traces],
                  "annotation": annotation_view(annotation), "automatic_tags": automatic, "evaluation_issues": issues,
                  "version_verification": verify_versions(run.snapshot, [selected_summary])}, run, score)


def save_annotation(session, case_id, data):
    case = _history(session, CaseRun, case_id)
    run = _history(session, EvaluationRun, case.run_id)
    score = resolve_score_set(session, run.id, data.score_set_id)
    row = session.scalar(select(BadcaseAnnotation).where(BadcaseAnnotation.case_run_id == case.id, BadcaseAnnotation.score_set_id == score.id))
    values = _safe({"manual_tags": data.manual_tags, "note": data.note}, run, score)
    if row is None:
        row = BadcaseAnnotation(run_id=run.id, case_run_id=case.id, score_set_id=score.id)
        session.add(row)
    row.manual_tags, row.note, row.updated_at = values["manual_tags"], values["note"], utcnow()
    session.commit()
    return _safe({"run_id": run.id, "case_run_id": case.id, "score_set_id": score.id, **annotation_view(row)}, run, score)


def _link(run_id, score_id, **params):
    return "/evaluations?" + urlencode({"run_id": run_id, "score_set_id": score_id, **params})


def _chart(title, data, reason=None, *, value_keys=("value",)):
    available = any(any(item.get(key) is not None for key in value_keys) for item in data)
    return {"title": title, "available": available, "reason": None if available else reason, "data": data}


def report(session, run_id, score_set_id=None, compare_run_id=None, compare_score_set_id=None):
    run, score, cases, side = comparison_side(session, run_id, score_set_id)
    projection = run_projection(session, run, score)
    categories = []
    for category in AUTOMATIC_TAGS:
        ids = [row["id"] for row in projection if category in row["automatic_tags"]]
        if ids:
            categories.append({"name": category, "value": len(ids), "case_run_ids": ids,
                               "link": _link(run.id, score.id, category=category)})
    distribution = []
    for lower, upper, name in [(0, 1000, "< 1 s"), (1000, 5000, "1–5 s"), (5000, 15000, "5–15 s"), (15000, 60000, "15–60 s"), (60000, None, "≥ 60 s")]:
        ids = [row["id"] for row in projection if row["duration_ms"] is not None and row["duration_ms"] >= lower and (upper is None or row["duration_ms"] < upper)]
        if ids:
            distribution.append({"name": name, "value": len(ids), "lower_ms": lower, "upper_ms": upper,
                                 "case_run_ids": ids, "link": _link(run.id, score.id, case_run_ids=",".join(ids))})
    if compare_score_set_id is not None and compare_run_id is None:
        raise ApiError(422, "INVALID_FILTER", "选择比较评分集时须同时选择比较运行", [{"field": "compare_run_id", "code": "required"}])
    comparison = compare_runs(session, run.id, compare_run_id, score.id, compare_score_set_id) if compare_run_id is not None else None
    comparison_data, changes = [], []
    if comparison:
        for metric in comparison["metrics"]:
            if metric["metric"] not in {"correctness", "relevance", "task_completion", "faithfulness"}:
                continue
            ids = metric["left_case_run_ids"]
            row = {"name": metric["metric"], "value": metric["left_mean"], "left_value": metric["left_mean"],
                   "right_value": metric["right_mean"], "paired_count": metric["paired_count"], "reason": metric["reason"],
                   "mean_scope": metric["mean_scope"], "left_valid_count": metric["left_valid_count"], "right_valid_count": metric["right_valid_count"],
                   "case_run_ids": ids, "right_case_run_ids": metric["right_case_run_ids"],
                   "link": _link(run.id, score.id, case_run_ids=",".join(ids)) if ids else _link(run.id, score.id, metric=metric["metric"]),
                   "right_link": _link(comparison["right"]["run"]["id"], comparison["right"]["score_set"]["id"], case_run_ids=",".join(metric["right_case_run_ids"])) if metric["right_case_run_ids"] else None}
            comparison_data.append(row)
            changes.append({**row, "value": metric["delta"]})
    sets = list(session.scalars(select(ScoreSet).where(ScoreSet.run_id == run.id).order_by(ScoreSet.created_at, ScoreSet.id)))
    result = {**side, "score_sets": [public_row(item, SCORE_FIELDS) for item in sets],
              "source_scope": side["summary"]["source_scope"], "version_verification": verify_versions(run.snapshot, cases),
              "charts": {"experiment_comparison": _chart("实验指标对比", comparison_data, "NO_VALID_SCORES" if comparison else "SELECT_SECOND_RUN", value_keys=("left_value", "right_value")),
                         "metric_changes": _chart("共同有效样本指标变化", changes, "CONDITIONS_MISMATCH" if comparison and not comparison["comparable"] else "NO_COMMON_VALID_SCORES" if comparison else "SELECT_SECOND_RUN"),
                         "badcase_categories": _chart("Badcase 分类（可多标签）", categories, "NO_AUTOMATIC_BADCASES"),
                         "latency_distribution": _chart("成功执行耗时分布", distribution, "NO_SUCCESSFUL_EXECUTIONS")},
              "comparison": comparison}
    return _safe(result, run, score)


def dashboard(session, source_type="REAL_EXTERNAL"):
    runs = list(session.scalars(select(EvaluationRun).order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())))
    counts = Counter(run.snapshot.get("agent", {}).get("source_type", "UNKNOWN") for run in runs)
    recent = []
    selected = None
    for run in runs:
        source = run.snapshot.get("agent", {}).get("source_type", "UNKNOWN")
        score = _score_set(session, run.id, None)
        if len(recent) < 20:
            recent.append({**public_row(run, "id experiment_id status total_cases created_at"), "score_set_id": score.id,
                           "source_type": source, "candidate_version_label": run.snapshot.get("candidate_version_label", ""),
                           "report_link": "/reports?" + urlencode({"run_id": run.id, "score_set_id": score.id})})
        if selected is None and (source_type == "ALL" or source == source_type):
            selected = run
    summary = comparison_side(session, selected.id)[3]["summary"] if selected else None
    return _safe({"source_scope": {"selected_source_type": source_type, "counts": dict(counts),
                                   "summary_scope": "LATEST_MATCHING_RUN_ONLY", "recent_runs_scope": "ALL_SOURCES_WITH_LABELS",
                                   "reason": None if selected else "NO_RUN_IN_SELECTED_SOURCE_SCOPE"},
                  "recent_runs": recent, "summary": summary, "selected_run_id": selected.id if selected else None})
