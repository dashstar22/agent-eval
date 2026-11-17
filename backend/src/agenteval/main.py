"""本机管理与运行API；真实批次限定单进程并持有独立schema锁。"""

import os
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .db import make_engine, session_factory
from .errors import ApiError
from .persistence import AgentConfig, CaseRun, Dataset, EvaluationRun, EvaluationScore, TestCase
from .privacy import known_secrets, sanitize
from .schemas import AgentCreate, AgentPatch, CaseCreate, CasePatch, DatasetCreate, DatasetPatch, FreezeRequest, Input, METRICS, canonical_id
from .services import (
    active, agent_view, case_run_view, case_view, dataset_view, locate_case, page_of,
    patch_agent, patch_case, public_row, report_input, run_view, soft_delete, write_case,
)
from .snapshots import freeze_run


def get_session(request: Request):
    factory = request.app.state.session_factory
    if factory is None:
        raise ApiError(503, "DATABASE_NOT_CONFIGURED", "请配置独立评测数据库并执行迁移")
    with factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise


def history(session, cls, identity):
    try:
        canonical_id(identity)
    except ValueError:
        raise ApiError(422, "INVALID_RESOURCE_ID", "资源标识须为规范 UUID") from None
    row = session.get(cls, identity)
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "历史资源不存在")
    return row


def create_app(engine=None, *, runtime_enabled=True, runtime_worker=None, runtime_executor_factory=None,
               runtime_on_ownership_lost=None):
    if engine is None and os.environ.get("AGENTEVAL_DATABASE_URL"):
        from .database_url import database_url
        engine = make_engine(database_url())

    def request_server_shutdown():
        # 标准Uvicorn启动器在主线程安装绑定Server的信号handler。
        # 直接请求正常关闭，避免os._exit丢弃已提交回答后的诊断收尾。
        import signal
        import uvicorn
        server = getattr(signal.getsignal(signal.SIGINT), "__self__", None)
        if isinstance(server, uvicorn.Server):
            server.should_exit = True

    @asynccontextmanager
    async def lifespan(app):
        manager = None
        if runtime_enabled and engine is not None:
            from .runtime_control import RuntimeManager
            manager = RuntimeManager(engine, app.state.session_factory, worker=runtime_worker,
                                     executor_factory=runtime_executor_factory,
                                     on_ownership_lost=runtime_on_ownership_lost or request_server_shutdown)
            manager.start()
            app.state.runtime_manager = manager
        try:
            yield
        finally:
            if manager is not None:
                manager.close()
            app.state.runtime_manager = None

    app = FastAPI(title="AgentEval 管理与运行API", version="0.4.0",
                  description="独立评测库、单活动批次、分阶段持久化；模拟及真实来源分别记录", lifespan=lifespan)
    app.state.session_factory = session_factory(engine) if engine is not None else None
    app.state.runtime_manager = None

    @app.middleware("http")
    async def trace_request(request, call_next):
        request.state.trace_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    def error_response(request, status, code, message, fields=None):
        trace = getattr(request.state, "trace_id", str(uuid4()))
        content = sanitize({"code": code, "message": message, "trace_id": trace, "fields": fields or []}, known_secrets())
        return JSONResponse(status_code=status, content=content, headers={"X-Trace-Id": trace})

    @app.exception_handler(ApiError)
    async def business_error(request, exc):
        return error_response(request, exc.status, exc.code, exc.message, exc.fields)

    from .runtime_control import RuntimeOwnershipLost
    @app.exception_handler(RuntimeOwnershipLost)
    async def runtime_ownership_error(request, exc):
        return error_response(request, 503, "RUNTIME_OWNERSHIP_LOST", "执行进程已失去独占锁，正在停止")

    async def validation_error(request, exc):
        # 不使用exc.json()，其input/ctx可能含原始密码和异常正文。
        known_fields = {name for model in Input.__subclasses__() for name in model.model_fields} | METRICS | {"body", "query", "path", "page", "page_size", "dataset_id", "config_id", "request_id", "enabled", "tag", "1", "2", "3", "4", "5"}
        fields = [{"field": ".".join(str(p) if isinstance(p, int) or p in known_fields else "<key>" for p in e["loc"]), "code": e["type"], "message": "字段缺失、类型或取值不合法"} for e in exc.errors()]
        return error_response(request, 422, "VALIDATION_ERROR", "输入校验失败", fields)

    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(ValidationError, validation_error)

    @app.exception_handler(IntegrityError)
    async def conflict_error(request, exc):
        return error_response(request, 409, "DATA_CONFLICT", "标识重复或关联约束冲突")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return error_response(request, 503, "DATABASE_ERROR", "数据库操作失败，请核对迁移和连接状态")

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return error_response(request, 500, "INTERNAL_ERROR", "内部处理失败，请使用 trace_id 排查")

    # FastAPI默认404/405也遵循统一错误契约。
    from starlette.exceptions import HTTPException
    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, exc.status_code, "HTTP_ERROR", "请求的资源或方法不可用")

    router = APIRouter(prefix="/api/v1")

    @router.get("/health")
    def health(request: Request):
        manager = request.app.state.runtime_manager
        available = manager is not None and manager.started and not manager._stopping
        return {"status": "OK", "capabilities": ["datasets", "agent-configs", "freeze-only", "single-process-runtime"],
                "execution_available": available}

    @router.get("/datasets")
    def datasets(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), session=Depends(get_session)):
        return page_of(session, select(Dataset).where(Dataset.deleted_at.is_(None)).order_by(Dataset.id), page, page_size, lambda r: dataset_view(session, r))

    @router.post("/datasets", status_code=201)
    def create_dataset(data: DatasetCreate, response: Response, session=Depends(get_session)):
        row = Dataset(**sanitize(data.model_dump(), known_secrets()))
        session.add(row)
        session.commit()
        response.headers["Location"] = f"/api/v1/datasets/{row.id}"
        return dataset_view(session, row)

    @router.get("/datasets/{dataset_id}")
    def dataset(dataset_id: int, session=Depends(get_session)):
        return dataset_view(session, active(session, Dataset, dataset_id))

    @router.patch("/datasets/{dataset_id}")
    def update_dataset(dataset_id: int, data: DatasetPatch, session=Depends(get_session)):
        row = active(session, Dataset, dataset_id)
        valid = DatasetCreate.model_validate(public_row(row, "name description") | data.model_dump(exclude_unset=True))
        for key, value in sanitize(valid.model_dump(), known_secrets()).items():
            setattr(row, key, value)
        row.revision += 1
        session.commit()
        return dataset_view(session, row)

    @router.delete("/datasets/{dataset_id}", status_code=204)
    def delete_dataset(dataset_id: int, session=Depends(get_session)):
        soft_delete(session, active(session, Dataset, dataset_id))
        session.commit()
        return Response(status_code=204)

    @router.get("/datasets/{dataset_id}/cases")
    def cases(dataset_id: int, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), enabled: bool | None = None, tag: str | None = Query(None, max_length=200), session=Depends(get_session)):
        active(session, Dataset, dataset_id)
        stmt = select(TestCase).where(TestCase.dataset_id == dataset_id, TestCase.deleted_at.is_(None))
        if enabled is not None:
            stmt = stmt.where(TestCase.enabled == enabled)
        if tag is not None:
            import json
            from sqlalchemy import exists, func
            if session.bind.dialect.name == "mysql":
                stmt = stmt.where(func.json_contains(TestCase.tags, json.dumps(tag, ensure_ascii=False)))
            else:
                tags = func.json_each(TestCase.tags).table_valued("value")
                stmt = stmt.where(exists(select(1).select_from(tags).where(tags.c.value == tag)))
        return page_of(session, stmt.order_by(TestCase.case_key), page, page_size, case_view)

    @router.post("/datasets/{dataset_id}/cases", status_code=201)
    def create_case(dataset_id: int, data: CaseCreate, response: Response, session=Depends(get_session)):
        row = write_case(session, active(session, Dataset, dataset_id), data)
        session.commit()
        response.headers["Location"] = f"/api/v1/datasets/{dataset_id}/cases/{row.case_key}"
        return case_view(row)

    @router.get("/datasets/{dataset_id}/cases/{case_key}")
    def case(dataset_id: int, case_key: str, session=Depends(get_session)):
        return case_view(locate_case(session, dataset_id, case_key))

    @router.patch("/datasets/{dataset_id}/cases/{case_key}")
    def update_case(dataset_id: int, case_key: str, data: CasePatch, session=Depends(get_session)):
        row = patch_case(session, locate_case(session, dataset_id, case_key), data)
        session.commit()
        return case_view(row)

    @router.delete("/datasets/{dataset_id}/cases/{case_key}", status_code=204)
    def delete_case(dataset_id: int, case_key: str, session=Depends(get_session)):
        soft_delete(session, locate_case(session, dataset_id, case_key))
        session.commit()
        return Response(status_code=204)

    @router.get("/agent-configs")
    def agents(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), session=Depends(get_session)):
        return page_of(session, select(AgentConfig).where(AgentConfig.deleted_at.is_(None)).order_by(AgentConfig.id), page, page_size, agent_view)

    @router.post("/agent-configs", status_code=201)
    def create_agent(data: AgentCreate, response: Response, session=Depends(get_session)):
        values = data.model_dump()
        refs = values.pop("credential_refs")
        row = AgentConfig(**sanitize(values, known_secrets(refs)), credential_refs=refs)
        session.add(row)
        session.commit()
        response.headers["Location"] = f"/api/v1/agent-configs/{row.id}"
        return agent_view(row)

    @router.get("/agent-configs/{config_id}")
    def agent(config_id: int, session=Depends(get_session)):
        return agent_view(active(session, AgentConfig, config_id))

    @router.patch("/agent-configs/{config_id}")
    def update_agent(config_id: int, data: AgentPatch, session=Depends(get_session)):
        row = patch_agent(session, active(session, AgentConfig, config_id), data)
        session.commit()
        return agent_view(row)

    @router.delete("/agent-configs/{config_id}", status_code=204)
    def delete_agent(config_id: int, session=Depends(get_session)):
        soft_delete(session, active(session, AgentConfig, config_id))
        session.commit()
        return Response(status_code=204)

    @router.post("/frozen-runs", status_code=201)
    def freeze(data: FreezeRequest, response: Response, session=Depends(get_session)):
        run = freeze_run(session, data)
        session.commit()
        response.headers["Location"] = f"/api/v1/runs/{run.id}"
        return run_view(run)

    @router.get("/runs/{run_id}")
    def run(run_id: str, session=Depends(get_session)):
        from .runtime_api import runtime_run_view
        return runtime_run_view(session, history(session, EvaluationRun, run_id))

    @router.get("/runs/{run_id}/cases")
    def run_cases(run_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), session=Depends(get_session)):
        history(session, EvaluationRun, run_id)
        return page_of(session, select(CaseRun).where(CaseRun.run_id == run_id).order_by(CaseRun.case_key), page, page_size, case_run_view)

    @router.get("/case-runs/{case_run_id}")
    def case_run(case_run_id: str, session=Depends(get_session)):
        row = history(session, CaseRun, case_run_id)
        result = case_run_view(row)
        scores = session.scalars(select(EvaluationScore).where(EvaluationScore.case_run_id == row.id).order_by(EvaluationScore.score_set_id, EvaluationScore.metric)).all()
        result["scores"] = sanitize([public_row(s, "id run_id score_set_id case_run_id metric status score required reason evidence") for s in scores], known_secrets())
        return result

    @router.get("/runs/{run_id}/report-input")
    def frozen_report_input(run_id: str, session=Depends(get_session)):
        return report_input(session, history(session, EvaluationRun, run_id))

    from .scoring_api import add_scoring_routes
    from .runtime_api import add_runtime_routes
    from .experiment_api import add_experiment_routes
    from .workbench_api import add_workbench_routes
    add_scoring_routes(router, get_session)
    add_runtime_routes(router)
    add_experiment_routes(router, get_session)
    add_workbench_routes(router, get_session)
    app.include_router(router)
    return app


app = create_app()
