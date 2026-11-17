"""已保存评分的只读入口；不触发 Agent、Judge 或后台任务。"""

from fastapi import Depends
from sqlalchemy import select

from .errors import ApiError
from .persistence import CaseEvaluation, CaseRun, EvaluationRun, EvaluationScore, JudgeAttempt, ScoreSet
from .privacy import known_secrets, sanitize
from .schemas import RuleProfile, canonical_id
from .services import public_row


def _history(session, cls, identity):
    try:
        canonical_id(identity)
    except ValueError:
        raise ApiError(422, "INVALID_RESOURCE_ID", "资源标识须为规范 UUID") from None
    row = session.get(cls, identity)
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "历史资源不存在")
    return row


def _score_set(session, run_id, score_set_id):
    if score_set_id:
        row = _history(session, ScoreSet, score_set_id)
        if row.run_id != run_id:
            raise ApiError(404, "RESOURCE_NOT_FOUND", "该运行不存在此评分集")
        return row
    row = session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id, ScoreSet.kind == "ORIGINAL"))
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "评分集不存在")
    return row


def add_scoring_routes(router, get_session):
    @router.get("/runs/{run_id}/scoring-summary")
    def scoring_summary(run_id: str, score_set_id: str | None = None, session=Depends(get_session)):
        from .evaluation_store import load_summary_cases
        from .statistics import summarize

        run = _history(session, EvaluationRun, run_id)
        score_set = _score_set(session, run.id, score_set_id)
        keys = [case["case_key"] for case in run.snapshot["cases"]]
        result = summarize(keys, load_summary_cases(session, run.id, score_set.id),
                           run_status=score_set.status if score_set.kind == "RESCORE" else run.status,
                           rule_profile=RuleProfile.model_validate(dict(score_set.rule_snapshot)))
        return sanitize({"run_id": run.id, "score_set_id": score_set.id,
                         "fingerprint": score_set.fingerprint, "summary": result}, known_secrets())

    @router.get("/case-runs/{case_run_id}/scoring")
    def case_scoring(case_run_id: str, score_set_id: str | None = None, session=Depends(get_session)):
        case = _history(session, CaseRun, case_run_id)
        score_set = _score_set(session, case.run_id, score_set_id)
        conditions = {"run_id": case.run_id, "score_set_id": score_set.id, "case_run_id": case.id}
        evaluation = session.scalar(select(CaseEvaluation).filter_by(**conditions))
        metrics = session.scalars(select(EvaluationScore).filter_by(**conditions).order_by(EvaluationScore.metric)).all()
        attempts = session.scalars(select(JudgeAttempt).filter_by(**conditions).order_by(JudgeAttempt.attempt)).all()
        result = {"run_id": case.run_id, "case_run_id": case.id, "score_set_id": score_set.id,
                  "agent_status": case.agent_status,
                  "evaluation": public_row(evaluation, "status conclusion completed_at") if evaluation else None,
                  "metrics": [public_row(row, "metric status score required reason evidence") for row in metrics],
                  "judge_attempts": [public_row(row, "attempt status request response usage duration_ms error") for row in attempts]}
        return sanitize(result, known_secrets())
