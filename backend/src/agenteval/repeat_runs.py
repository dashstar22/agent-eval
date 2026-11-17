"""顺序重复冻结计划；同一活动槽、每轮独立事实，绝不自动续跑。"""

import json
from uuid import uuid4

from sqlalchemy import select, update

from .db import utcnow
from .errors import ApiError
from .persistence import AgentConfig, CaseRun, EvaluationRun, RuntimeControl, ScoreSet
from .privacy import known_secrets, sanitize
from .repeat_models import RepeatBatch, RepeatMember
from .repeat_schemas import RepeatRequest
from .schemas import ExecutionBudget, canonical_id
from .snapshots import canonical_json, digest

ACTIVE = ("QUEUED", "RUNNING")


def get_batch(session, identity):
    try:
        canonical_id(identity)
    except ValueError:
        raise ApiError(422, "INVALID_RESOURCE_ID", "批次标识须为规范 UUID") from None
    row = session.get(RepeatBatch, identity)
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "重复运行批次不存在")
    return row


def batch_view(session, batch):
    rows = session.execute(select(RepeatMember, EvaluationRun, ScoreSet).join(
        EvaluationRun, EvaluationRun.id == RepeatMember.run_id
    ).join(ScoreSet, (ScoreSet.run_id == EvaluationRun.id) & (ScoreSet.kind == "ORIGINAL")).where(
        RepeatMember.batch_id == batch.id
    ).order_by(RepeatMember.ordinal)).all()
    return sanitize({
        "id": batch.id, "source_run_id": batch.source_run_id, "repeat_count": batch.repeat_count,
        "status": batch.status, "created_at": batch.created_at, "started_at": batch.started_at,
        "ended_at": batch.ended_at, "budget": batch.budget, "error": batch.error,
        "runs": [{"id": run.id, "ordinal": member.ordinal, "status": run.status, "score_set_id": score.id}
                 for member, run, score in rows],
    }, known_secrets())


def _budget(source, count):
    snapshot = source.snapshot
    budget = ExecutionBudget.model_validate(dict(snapshot["execution_budget"]))
    real = snapshot["agent"].get("source_type") == "REAL_EXTERNAL" or bool(
        snapshot["judge"].get("base_url") and snapshot["judge"].get("source_type", "REAL_EXTERNAL") == "REAL_EXTERNAL"
    )
    if real and (budget.max_total_calls is None or budget.total_timeout_seconds is None):
        raise ApiError(422, "REAL_EXECUTION_BUDGET_REQUIRED", "重复真实执行须使用已有明确的每轮调用和时长预算")
    seconds = budget.total_timeout_seconds or budget.case_timeout_seconds * source.total_cases
    return {"real_execution": real, "per_run_max_calls": budget.max_total_calls,
            "total_max_calls": None if budget.max_total_calls is None else budget.max_total_calls * count,
            "per_run_timeout_seconds": seconds, "total_timeout_seconds": seconds * count}


def accept_repeat(manager, request):
    request = RepeatRequest.model_validate(request)
    if not manager.started or manager._stopping:
        raise ApiError(503, "RUNTIME_UNAVAILABLE", "后台执行器未启动")
    with manager._accept_guard:
        manager.ensure_owner()
        payload_hash = digest(request.model_dump(exclude={"request_id"}))
        with manager.session_factory() as session, session.begin():
            slot = session.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())
            if slot is None:
                raise ApiError(503, "RUNTIME_SLOT_MISSING", "活动槽缺失，请执行迁移")
            existing = session.scalar(select(RepeatBatch).where(RepeatBatch.request_id == request.request_id))
            if existing is not None:
                if existing.request_sha256 != payload_hash:
                    raise ApiError(409, "REQUEST_ID_PAYLOAD_MISMATCH", "同一请求号不能改变重复执行参数")
                return batch_view(session, existing) | {"idempotent_replay": True}
            if slot.active_job_id:
                raise ApiError(409, "RUNTIME_BUSY", "已有活动任务，请等待完成后再提交重复执行")
            source = session.get(EvaluationRun, request.source_run_id)
            if source is None:
                raise ApiError(404, "RESOURCE_NOT_FOUND", "来源运行不存在")
            if source.status != "COMPLETED":
                raise ApiError(409, "SOURCE_RUN_NOT_COMPLETED", "请使用已完成运行的冻结配置")
            if not 1 <= source.total_cases <= 1000:
                raise ApiError(422, "RUNTIME_CASE_LIMIT", "重复执行须有 1 至 1000 条冻结用例")
            agent = session.get(AgentConfig, source.snapshot["agent"]["id"])
            if agent is None or agent.deleted_at is not None or not agent.enabled:
                raise ApiError(422, "AGENT_CONFIG_DISABLED", "来源 Agent 配置已停用或删除")
            budget = _budget(source, request.repeat_count)
            if budget["real_execution"] and not request.confirm_real_execution:
                raise ApiError(422, "REAL_REPEAT_CONFIRMATION_REQUIRED", "重复执行会重新调用 Agent 和 Judge，请明确确认次数及合计预算")
            batch = RepeatBatch(request_id=request.request_id, request_sha256=payload_hash,
                source_run_id=source.id, repeat_count=request.repeat_count,
                owner_instance=manager.owner_instance, budget=budget)
            session.add(batch)
            session.flush()
            for ordinal in range(1, request.repeat_count + 1):
                snapshot = json.loads(canonical_json(source.snapshot))
                # 保留完整原配置；批次来源是独立关系，不注入可比性字段。
                run = EvaluationRun(experiment_id=source.experiment_id, request_id=str(uuid4()),
                    request_sha256=digest({"batch_id": batch.id, "ordinal": ordinal}),
                    snapshot=snapshot, snapshot_sha256=digest(snapshot), status="QUEUED",
                    owner_instance=manager.owner_instance, total_cases=source.total_cases, observed_versions={"verification": "UNKNOWN"})
                session.add(run)
                session.flush()
                session.add(ScoreSet(run_id=run.id, kind="ORIGINAL", judge_snapshot=snapshot["judge"],
                    rule_snapshot=snapshot["rules"], fingerprint=digest({"judge": snapshot["judge"], "rules": snapshot["rules"]}), status="PENDING"))
                for content in snapshot["cases"]:
                    session.add(CaseRun(run_id=run.id, case_key=content["case_key"], case_snapshot=content, phase="PENDING"))
                session.add(RepeatMember(batch_id=batch.id, run_id=run.id, ordinal=ordinal))
            slot.active_job_type, slot.active_job_id, slot.owner_instance = "REPEAT", batch.id, manager.owner_instance
            session.flush()
            manager.ensure_owner()
            result = batch_view(session, batch) | {"idempotent_replay": False}
        try:
            manager.executor.submit(dispatch_repeat, manager, result["id"])
        except Exception:
            finish_repeat(manager.session_factory, result["id"], manager.owner_instance, "FAILED", "RUNTIME_REGISTRATION_FAILED")
            raise ApiError(503, "RUNTIME_REGISTRATION_FAILED", "重复批次注册失败，已保留失败记录") from None
        return result


def finish_repeat(factory, identity, owner, status, code=None):
    with factory() as session, session.begin():
        # 顺序统一为活动槽、批次、成员；旧执行器不能覆盖新所有者的恢复结果。
        slot = session.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())
        batch = session.scalar(select(RepeatBatch).where(RepeatBatch.id == identity).with_for_update())
        if batch is None or batch.owner_instance != owner or batch.status not in ACTIVE:
            return
        batch.status, batch.ended_at = status, utcnow()
        batch.error = {"code": code} if code else None
        run_ids = list(session.scalars(select(RepeatMember.run_id).join(
            EvaluationRun, EvaluationRun.id == RepeatMember.run_id
        ).where(RepeatMember.batch_id == identity, EvaluationRun.owner_instance == owner)))
        session.execute(update(EvaluationRun).where(EvaluationRun.id.in_(run_ids), EvaluationRun.owner_instance == owner,
            EvaluationRun.status.in_(ACTIVE)).values(status="INTERRUPTED", ended_at=utcnow(), error={"code": code or "REPEAT_STOPPED"}))
        session.execute(update(ScoreSet).where(ScoreSet.run_id.in_(run_ids), ScoreSet.kind == "ORIGINAL",
            ScoreSet.status.in_(("QUEUED", "RUNNING", "PENDING", "FROZEN"))).values(status="INTERRUPTED"))
        session.execute(update(CaseRun).where(CaseRun.run_id.in_(run_ids), CaseRun.phase == "PENDING").values(phase="INTERRUPTED"))
        if slot and slot.active_job_id == identity and slot.owner_instance == owner:
            slot.active_job_id = slot.active_job_type = slot.owner_instance = None


def dispatch_repeat(manager, identity):
    try:
        manager.ensure_owner()
        with manager.session_factory() as session, session.begin():
            slot = session.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())
            if slot is None or slot.active_job_id != identity or slot.owner_instance != manager.owner_instance:
                return
            batch = session.scalar(select(RepeatBatch).where(RepeatBatch.id == identity).with_for_update())
            if batch is None or batch.owner_instance != manager.owner_instance or batch.status != "QUEUED":
                return
            changed = session.execute(update(RepeatBatch).where(
                RepeatBatch.id == identity, RepeatBatch.owner_instance == manager.owner_instance,
                RepeatBatch.status == "QUEUED",
            ).values(status="RUNNING", started_at=utcnow()))
            if changed.rowcount != 1:
                return
            run_ids = list(session.scalars(select(RepeatMember.run_id).where(RepeatMember.batch_id == identity).order_by(RepeatMember.ordinal)))
            manager.ensure_owner()
        for run_id in run_ids:
            manager.ensure_owner()
            manager._dispatch(run_id)
            with manager.session_factory() as session:
                status = session.get(EvaluationRun, run_id).status
            if status != "COMPLETED":
                finish_repeat(manager.session_factory, identity, manager.owner_instance,
                              "INTERRUPTED" if status == "INTERRUPTED" else "FAILED", "REPEAT_MEMBER_NOT_COMPLETED")
                return
        finish_repeat(manager.session_factory, identity, manager.owner_instance, "COMPLETED")
    except Exception:
        finish_repeat(manager.session_factory, identity, manager.owner_instance, "INTERRUPTED", "REPEAT_EXECUTION_INTERRUPTED")


def recover_repeat_batches(session, owner):
    # 仅在 RuntimeManager 已取得 schema 租约后调用；不重新排队。
    for batch in session.scalars(select(RepeatBatch).where(RepeatBatch.status.in_(ACTIVE))):
        batch.status, batch.ended_at = "INTERRUPTED", utcnow()
        batch.error = {"code": "PROCESS_INTERRUPTED", "message": "原进程已退出，重复批次不会自动续跑"}
