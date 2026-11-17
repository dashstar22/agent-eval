"""重复批次提交和进度读取。"""

from fastapi import Depends, Query, Request
from sqlalchemy import select

from .errors import ApiError
from .repeat_models import RepeatBatch
from .repeat_runs import accept_repeat, batch_view, get_batch
from .repeat_schemas import RepeatRequest


def add_repeat_routes(router, get_session):
    @router.post("/extensions/repeat-batches", status_code=202)
    def start(data: RepeatRequest, request: Request):
        manager = request.app.state.runtime_manager
        if manager is None:
            raise ApiError(503, "RUNTIME_UNAVAILABLE", "后台执行器未启动，请启动完整工作台")
        return accept_repeat(manager, data)

    @router.get("/extensions/repeat-batches")
    def batches(limit: int = Query(20, ge=1, le=100), session=Depends(get_session)):
        rows = session.scalars(select(RepeatBatch).order_by(RepeatBatch.created_at.desc(), RepeatBatch.id.desc()).limit(limit))
        return {"items": [batch_view(session, row) for row in rows]}

    @router.get("/extensions/repeat-batches/{batch_id}")
    def batch(batch_id: str, session=Depends(get_session)):
        return batch_view(session, get_batch(session, batch_id))
