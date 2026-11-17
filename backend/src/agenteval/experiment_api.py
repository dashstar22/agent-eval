"""实验维护、运行历史和受同一执行器约束的重评分 HTTP 入口。"""

from fastapi import Depends, Query, Request, Response
from sqlalchemy import select

from .errors import ApiError
from .experiment_schemas import ExperimentCreate, ExperimentPatch, ExperimentStart, RescoreRequest
from .experiments import check_experiment_inputs, experiment_view, run_list_view, score_set_view
from .persistence import EvaluationRun, Experiment, ScoreSet
from .privacy import known_secrets, sanitize
from .runtime_schemas import StartRunRequest
from .scoring_api import _history
from .services import active, page_of, public_row, soft_delete


def manager_for(request):
    manager = request.app.state.runtime_manager
    if manager is None:
        raise ApiError(503, "RUNTIME_UNAVAILABLE", "后台执行器未启动，请使用单进程并配置独立数据库")
    return manager


def add_experiment_routes(router, get_session):
    @router.get("/experiments")
    def experiments(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), session=Depends(get_session)):
        return page_of(session, select(Experiment).where(Experiment.deleted_at.is_(None)).order_by(Experiment.created_at.desc(), Experiment.id.desc()), page, page_size, experiment_view)

    @router.post("/experiments", status_code=201)
    def create_experiment(data: ExperimentCreate, response: Response, session=Depends(get_session)):
        check_experiment_inputs(session, data)
        row = Experiment(**sanitize(data.model_dump(), known_secrets()))
        session.add(row)
        session.commit()
        response.headers["Location"] = f"/api/v1/experiments/{row.id}"
        return experiment_view(row)

    @router.get("/experiments/{experiment_id}")
    def experiment(experiment_id: int, session=Depends(get_session)):
        row = active(session, Experiment, experiment_id)
        return experiment_view(row)

    @router.patch("/experiments/{experiment_id}")
    def patch_experiment(experiment_id: int, data: ExperimentPatch, session=Depends(get_session)):
        row = active(session, Experiment, experiment_id)
        fields = "name dataset_id agent_config_id version_label purpose"
        valid = ExperimentCreate.model_validate(public_row(row, fields) | data.model_dump(exclude_unset=True))
        check_experiment_inputs(session, valid)
        for key, value in sanitize(valid.model_dump(), known_secrets()).items():
            setattr(row, key, value)
        session.commit()
        return experiment_view(row)

    @router.delete("/experiments/{experiment_id}", status_code=204)
    def delete_experiment(experiment_id: int, session=Depends(get_session)):
        soft_delete(session, active(session, Experiment, experiment_id))
        session.commit()
        return Response(status_code=204)

    @router.get("/experiments/{experiment_id}/runs")
    def experiment_runs(experiment_id: int, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), session=Depends(get_session)):
        # 删除只阻止新运行，已删除实验的历史依旧可查。
        if session.get(Experiment, experiment_id) is None:
            raise ApiError(404, "RESOURCE_NOT_FOUND", "实验不存在")
        return page_of(session, select(EvaluationRun).where(EvaluationRun.experiment_id == experiment_id).order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc()), page, page_size, lambda row: run_list_view(session, row))

    @router.get("/runs")
    def runs(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), experiment_id: int | None = Query(None, gt=0), session=Depends(get_session)):
        stmt = select(EvaluationRun)
        if experiment_id is not None:
            stmt = stmt.where(EvaluationRun.experiment_id == experiment_id)
        return page_of(session, stmt.order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc()), page, page_size, lambda row: run_list_view(session, row))

    @router.post("/experiments/{experiment_id}/runs", status_code=202)
    def start_experiment(experiment_id: int, data: ExperimentStart, request: Request, response: Response, session=Depends(get_session)):
        row = active(session, Experiment, experiment_id)
        start = StartRunRequest(dataset_id=row.dataset_id, agent_config_id=row.agent_config_id,
                                version_label=row.version_label, **data.model_dump())
        result = manager_for(request).accept(start, experiment_id=experiment_id)
        response.headers["Location"] = f"/api/v1/runs/{result['run_id']}"
        return result

    @router.get("/runs/{run_id}/score-sets")
    def score_sets(run_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), session=Depends(get_session)):
        run = _history(session, EvaluationRun, run_id)
        return page_of(session, select(ScoreSet).where(ScoreSet.run_id == run.id).order_by(ScoreSet.created_at, ScoreSet.id), page, page_size, lambda row: score_set_view(session, row))

    @router.get("/score-sets/{score_set_id}")
    def score_set(score_set_id: str, session=Depends(get_session)):
        return score_set_view(session, _history(session, ScoreSet, score_set_id))

    @router.post("/score-sets", status_code=202)
    def rescore(data: RescoreRequest, request: Request, response: Response):
        result = manager_for(request).accept_rescore(data)
        response.headers["Location"] = f"/api/v1/score-sets/{result['id']}"
        return result
