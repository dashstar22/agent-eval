"""人工复核与回归候选闭环：只操作保存记录，不调用模型。"""

import json
from math import fsum, isfinite

from pydantic import ValidationError
from sqlalchemy import select

from .comparisons import resolve_score_set
from .db import utcnow
from .errors import ApiError
from .judge import applicable_quality_metrics
from .models import ExecutionResult
from .persistence import CaseEvaluation, CaseRun, Dataset, EvaluationRun, EvaluationScore
from .privacy import known_secrets, sanitize
from .review_models import HumanReview, RegressionDraft
from .review_schemas import QUALITY_METRICS
from .schemas import CaseCreate
from .scoring_api import _history
from .services import active, public_row, write_case

IDENTITY_NOTICE = "姓名为本机用户填报，未经身份认证；人工确认是用户声明，不代表独立第三方质量证明。"
REVIEW_FIELDS = "id run_id case_run_id score_set_id reviewer_name identity_type conclusion scores reason human_confirmed created_at"
DRAFT_FIELDS = "id run_id case_run_id score_set_id target_dataset_id case_key case_snapshot reason status approved_by approved_at test_case_id created_at"


def _safe(value, run=None, score=None):
    refs = dict(run.snapshot.get("agent", {}).get("credential_refs", {})) if run else {}
    if score:
        refs["judge_ref"] = score.judge_snapshot.get("credential_ref")
    return sanitize(value, known_secrets(refs))


def _case_scope(session, case_run_id, score_set_id):
    case = _history(session, CaseRun, case_run_id)
    run = _history(session, EvaluationRun, case.run_id)
    score = resolve_score_set(session, run.id, score_set_id)
    return case, run, score


def _run_scope(session, run_id, score_set_id):
    run = _history(session, EvaluationRun, run_id)
    return run, resolve_score_set(session, run.id, score_set_id)


def _metrics(session, case, score):
    return list(session.scalars(select(EvaluationScore).where(
        EvaluationScore.run_id == case.run_id, EvaluationScore.case_run_id == case.id,
        EvaluationScore.score_set_id == score.id,
    ).order_by(EvaluationScore.metric)))


def _applicable(case, metrics):
    names = set(case.case_snapshot.get("required_metrics", [])) | {item.metric for item in metrics if item.status != "NA"}
    # 必评是结论门槛，不能补出缺失的实际上下文。与 Judge 共用适用性判定，
    # 依据保存的归一执行事实，既不依赖 Judge 成败，也不把引用子集当完整上下文。
    names.discard("faithfulness")
    normalized = (case.raw_agent or {}).get("normalized_execution")
    try:
        execution = ExecutionResult.model_validate(normalized) if normalized is not None else None
    except ValidationError:
        execution = None
    if (execution is None or execution.answer is None or not case.case_snapshot.get("question")
            or not case.case_snapshot.get("expected_answer")):
        return []
    if "faithfulness" in applicable_quality_metrics(execution):
        names.add("faithfulness")
    return [name for name in QUALITY_METRICS if name in names]


def review_case_list(session, run_id, score_set_id=None):
    """盲审选择器只投影问题和身份，不通过普通用例列表泄露结论。"""
    run, score = _run_scope(session, run_id, score_set_id)
    cases = session.scalars(select(CaseRun).where(CaseRun.run_id == run.id).order_by(CaseRun.case_key))
    items = [{"id": case.id, "case_key": case.case_key, "question": case.case_snapshot.get("question")}
             for case in cases]
    return _safe({"items": items, "run_id": run.id, "score_set_id": score.id}, run, score)


def review_case(session, case_run_id, score_set_id=None, *, blind=True):
    case, run, score = _case_scope(session, case_run_id, score_set_id)
    metrics = _metrics(session, case, score)
    snapshot, observations = case.case_snapshot, case.observations or {}
    result = {
        "blind": blind, "identity_notice": IDENTITY_NOTICE,
        "case": {"id": case.id, "run_id": case.run_id, "case_key": case.case_key,
                 "question": snapshot.get("question"), "expected_answer": snapshot.get("expected_answer"),
                 "answer": case.answer, "expected_tools": snapshot.get("expected_tools"),
                 "tool_match_mode": snapshot.get("tool_match_mode", "contains"),
                 "tool_assertions": snapshot.get("tool_assertions", {}),
                 "preconditions": snapshot.get("preconditions", {}),
                 "contexts": observations.get("contexts"),
                 "context_scope": observations.get("context_scope", "UNAVAILABLE"),
                 "context_completeness": observations.get("context_completeness", "UNAVAILABLE")},
        "score_set": public_row(score, "id kind status"), "applicable_metrics": _applicable(case, metrics),
        "judge": None,
    }
    if not blind:
        evaluation = session.scalar(select(CaseEvaluation).where(
            CaseEvaluation.run_id == run.id, CaseEvaluation.case_run_id == case.id, CaseEvaluation.score_set_id == score.id))
        result["judge"] = {"status": evaluation.status if evaluation else None,
                           "conclusion": evaluation.conclusion if evaluation else None,
                           "metrics": [public_row(item, "metric status score required reason evidence") for item in metrics]}
    return _safe(result, run, score)


def create_review(session, data):
    case, run, score = _case_scope(session, data.case_run_id, data.score_set_id)
    if not set(data.scores) <= set(_applicable(case, _metrics(session, case, score))):
        raise ApiError(422, "REVIEW_METRIC_NOT_APPLICABLE", "人工分数包含此冻结用例不适用的指标")
    values = _safe(data.model_dump(), run, score)
    row = HumanReview(run_id=run.id, case_run_id=case.id, score_set_id=score.id,
                      reviewer_name=values["reviewer_name"], conclusion=values["conclusion"],
                      scores=values["scores"], reason=values["reason"], human_confirmed=True,
                      identity_type="SELF_DECLARED")
    session.add(row)
    session.commit()
    return _safe(public_row(row, REVIEW_FIELDS), run, score)


def review_history(session, run_id, score_set_id=None):
    run, score = _run_scope(session, run_id, score_set_id)
    rows = list(session.scalars(select(HumanReview).where(
        HumanReview.run_id == run.id, HumanReview.score_set_id == score.id,
    ).order_by(HumanReview.created_at.desc(), HumanReview.id.desc())))
    latest = {}
    for row in rows:
        latest.setdefault(row.case_run_id, row)
    return _safe({"items": [public_row(row, REVIEW_FIELDS) for row in rows],
                  "latest_reviews": [public_row(row, REVIEW_FIELDS) for row in latest.values()],
                  "total": len(rows), "run_id": run.id, "score_set_id": score.id,
                  "identity_notice": IDENTITY_NOTICE}, run, score)


def _valid_score(value):
    return type(value) in {int, float} and isfinite(value) and value == int(value) and 1 <= value <= 5


def review_consistency(session, run_id, score_set_id=None):
    run, score = _run_scope(session, run_id, score_set_id)
    history = review_history(session, run.id, score.id)
    # 使用数据库原始数字计算；脱敏只在出界时进行，不能改变有效样本判断。
    rows = list(session.scalars(select(HumanReview).where(
        HumanReview.run_id == run.id, HumanReview.score_set_id == score.id,
    ).order_by(HumanReview.created_at.desc(), HumanReview.id.desc())))
    latest = {}
    for row in rows:
        latest.setdefault(row.case_run_id, row)
    cases = {case.id: case for case in session.scalars(select(CaseRun).where(CaseRun.run_id == run.id))}
    evaluations = {item.case_run_id: item for item in session.scalars(select(CaseEvaluation).where(
        CaseEvaluation.run_id == run.id, CaseEvaluation.score_set_id == score.id))}
    scores = {(item.case_run_id, item.metric): item for item in session.scalars(select(EvaluationScore).where(
        EvaluationScore.run_id == run.id, EvaluationScore.score_set_id == score.id))}
    pairs, disagreements = 0, []
    for case_id, human in latest.items():
        evaluation = evaluations.get(case_id)
        if human.conclusion not in {"PASS", "FAIL"} or not evaluation or evaluation.conclusion not in {"PASS", "FAIL"}:
            continue
        pairs += 1
        if human.conclusion != evaluation.conclusion:
            disagreements.append({"case_key": cases[case_id].case_key, "case_run_id": case_id,
                                  "human_conclusion": human.conclusion, "judge_conclusion": evaluation.conclusion})
    metrics = []
    for metric in QUALITY_METRICS:
        values, differing = [], []
        for case_id, human in latest.items():
            item, human_score = scores.get((case_id, metric)), human.scores.get(metric)
            if not item or item.status not in {"PASS", "FAIL"} or not _valid_score(item.score) or not _valid_score(human_score):
                continue
            values.append(abs(human_score - item.score))
            if human_score != item.score:
                differing.append({"case_key": cases[case_id].case_key, "case_run_id": case_id,
                                  "human_score": human_score, "judge_score": item.score})
        count = len(values)
        metrics.append({"metric": metric, "valid_pair_count": count,
                        "exact_agreement_count": count - len(differing),
                        "exact_agreement_rate": (count - len(differing)) / count if count else None,
                        "mean_absolute_difference": fsum(values) / count if count else None,
                        "disagreements": differing})
    return _safe({"run_id": run.id, "score_set_id": score.id, "review_count": history["total"],
                  "latest_review_count": len(latest), "identity_notice": IDENTITY_NOTICE,
                  "conclusions": {"valid_pair_count": pairs, "agreement_count": pairs - len(disagreements),
                                  "agreement_rate": (pairs - len(disagreements)) / pairs if pairs else None,
                                  "disagreements": disagreements}, "metrics": metrics}, run, score)


def create_draft(session, data):
    case, run, score = _case_scope(session, data.case_run_id, data.score_set_id)
    active(session, Dataset, data.target_dataset_id)
    from .workbench import detail
    projection = detail(session, case.id, score.id)
    latest_human_conclusion = session.scalar(select(HumanReview.conclusion).where(
        HumanReview.run_id == run.id, HumanReview.case_run_id == case.id,
        HumanReview.score_set_id == score.id,
    ).order_by(HumanReview.created_at.desc(), HumanReview.id.desc()).limit(1))
    if not (projection["automatic_tags"] or projection["evaluation_issues"]
            or (projection["scoring"]["evaluation"] or {}).get("conclusion") == "FAIL"
            or latest_human_conclusion == "FAIL"
            or projection["annotation"]["manual_tags"] or projection["annotation"]["note"]):
        raise ApiError(422, "REGRESSION_SOURCE_NOT_BADCASE", "来源用例没有失败、评测异常或人工复核标记")
    # 只从冻结用例复制配置；失败回答绝不成为参考答案。
    content = {name: case.case_snapshot[name] for name in CaseCreate.model_fields if name in case.case_snapshot}
    source = dict(content.get("source", {}))
    source["regression_origin"] = {"run_id": run.id, "case_run_id": case.id, "score_set_id": score.id,
                                   "reason": data.reason, "human_review_required": True}
    content.update(case_key=data.case_key, expected_answer=data.expected_answer, enabled=False,
                   source=source, seen_regression=True)
    validated = CaseCreate.model_validate(_safe(content, run, score)).model_dump()
    row = RegressionDraft(run_id=run.id, case_run_id=case.id, score_set_id=score.id,
                          target_dataset_id=data.target_dataset_id, case_key=data.case_key,
                          case_snapshot=validated, reason=_safe(data.reason, run, score), status="PENDING")
    session.add(row)
    session.commit()
    return _safe(public_row(row, DRAFT_FIELDS), run, score)


def draft_list(session, target_dataset_id=None):
    stmt = select(RegressionDraft).order_by(RegressionDraft.created_at.desc(), RegressionDraft.id.desc())
    if target_dataset_id is not None:
        active(session, Dataset, target_dataset_id)
        stmt = stmt.where(RegressionDraft.target_dataset_id == target_dataset_id)
    items = []
    for row in session.scalars(stmt):
        run = session.get(EvaluationRun, row.run_id)
        score = resolve_score_set(session, row.run_id, row.score_set_id)
        items.append(_safe(public_row(row, DRAFT_FIELDS), run, score))
    return {"items": items}


def approve_draft(session, draft_id, data):
    _history(session, RegressionDraft, draft_id)
    row = session.scalar(select(RegressionDraft).where(RegressionDraft.id == draft_id).with_for_update())
    run = session.get(EvaluationRun, row.run_id)
    score = resolve_score_set(session, row.run_id, row.score_set_id)
    if row.status == "APPROVED":
        return _safe(public_row(row, DRAFT_FIELDS), run, score)
    dataset = session.scalar(select(Dataset).where(Dataset.id == row.target_dataset_id).with_for_update())
    if not dataset or dataset.deleted_at is not None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "目标数据集不存在或已删除")
    # 候选从不在审批阶段修改；创建禁用管理用例和标记审批状态同一短事务提交。
    content = json.loads(json.dumps(dict(row.case_snapshot), ensure_ascii=False))
    content["enabled"], content["seen_regression"] = False, True
    content["source"]["regression_approval"] = {
        "draft_id": row.id, "reviewer_name": _safe(data.reviewer_name, run, score),
        "identity_type": "SELF_DECLARED", "human_confirmed": True,
    }
    case = write_case(session, dataset, CaseCreate.model_validate(content))
    row.status, row.test_case_id = "APPROVED", case.id
    row.approved_by, row.approved_at = _safe(data.reviewer_name, run, score), utcnow()
    session.commit()
    return _safe(public_row(row, DRAFT_FIELDS), run, score)
