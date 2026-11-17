"""运行提交和真实持久进度；读取不会触发外部执行。"""

from fastapi import Request, Response
from sqlalchemy import func, select

from .errors import ApiError
from .evaluation_store import load_summary_cases
from .persistence import CaseEvaluation, CaseRun, ScoreSet
from .privacy import known_secrets, sanitize
from .runtime_schemas import StartRunRequest
from .schemas import RuleProfile
from .services import public_row, run_view
from .statistics import summarize


def runtime_run_view(session, run):
    result = run_view(run)
    score_set = session.scalar(select(ScoreSet).where(ScoreSet.run_id == run.id, ScoreSet.kind == "ORIGINAL"))
    executed = session.scalar(select(func.count()).select_from(CaseRun).where(CaseRun.run_id == run.id, CaseRun.agent_status.is_not(None)))
    evaluated = session.scalar(select(func.count()).select_from(CaseEvaluation).where(CaseEvaluation.run_id == run.id, CaseEvaluation.score_set_id == score_set.id)) if score_set else 0
    result.update(public_row(run, "started_at ended_at error"))
    result["execution_available"] = run.status != "FROZEN"
    result["progress"] = {"total": run.total_cases, "executed": executed, "evaluated": evaluated,
                          "remaining": run.total_cases - evaluated, "complete": run.status == "COMPLETED"}
    if score_set:
        result["score_set_id"] = score_set.id
        result["summary"] = summarize([case["case_key"] for case in run.snapshot["cases"]],
                                      load_summary_cases(session, run.id, score_set.id), run_status=run.status,
                                      rule_profile=RuleProfile.model_validate(dict(score_set.rule_snapshot)))
    # snapshot在冻结时已经脱敏；引用字段名必须保留且摘要对应原保存内容。
    snapshot = result.pop("snapshot")
    result = sanitize(result, known_secrets())
    result["snapshot"] = snapshot
    return result


def add_runtime_routes(router):
    @router.post("/runs", status_code=202)
    def start_run(data: StartRunRequest, request: Request, response: Response):
        manager = request.app.state.runtime_manager
        if manager is None:
            raise ApiError(503, "RUNTIME_UNAVAILABLE", "后台执行器未启动，请使用单进程并配置独立数据库")
        result = manager.accept(data)
        response.headers["Location"] = f"/api/v1/runs/{result['run_id']}"
        return result
