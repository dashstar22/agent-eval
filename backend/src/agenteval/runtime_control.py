"""单进程独占、原子接受与孤立任务识别；不自动重放外部请求。"""

import hashlib
import os
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import select, update

from .db import utcnow
from .errors import ApiError
from .persistence import AgentConfig, CaseEvaluation, CaseRun, EvaluationRun, RuntimeControl, ScoreSet
from .runtime_schemas import StartRunRequest
from .schemas import FreezeRequest
from .snapshots import digest, freeze_run

ACTIVE_STATUSES = ("QUEUED", "RUNNING")


class RuntimeOwnershipLost(RuntimeError):
    def __init__(self):
        super().__init__("RUNTIME_OWNERSHIP_LOST")


class SchemaLease:
    """MySQL专属原始连接持有GET_LOCK；不会失联后自动重连或重新获取。"""

    def __init__(self, engine):
        self.engine = engine
        self._guard = threading.RLock()
        self._connection = None
        self._file = None
        self._lost = False
        self._acquired = False
        if engine.dialect.name == "mysql":
            identity = engine.url.database or ""
        elif engine.dialect.name == "sqlite":
            database = engine.url.database
            identity = str(Path(database).resolve()) if database and database != ":memory:" else f"memory-{os.getpid()}-{id(engine)}"
        else:
            raise RuntimeError("RUNTIME_DATABASE_BACKEND_UNSUPPORTED")
        self.name = "agenteval:" + hashlib.sha256(identity.encode()).hexdigest()[:48]

    def acquire(self):
        with self._guard:
            if self._acquired:
                raise RuntimeError("RUNTIME_LOCK_ALREADY_ACQUIRED")
            if self.engine.dialect.name == "mysql":
                connection = self.engine.raw_connection()
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT GET_LOCK(%s, 0)", (self.name,))
                        acquired = cursor.fetchone()[0]
                        if acquired != 1:
                            raise RuntimeError("RUNTIME_SCHEMA_LOCK_BUSY")
                        cursor.execute("SELECT CONNECTION_ID()")
                        self._connection_id = cursor.fetchone()[0]
                    self._connection = connection
                except BaseException:
                    connection.invalidate()
                    connection.close()
                    raise
            else:
                # 仅给SQLite测试提供真实跨进程互斥；正常部署仍要求独立MySQL。
                path = Path(tempfile.gettempdir()) / (self.name.replace(":", "-") + ".lock")
                handle = path.open("a+b")
                if path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    handle.close()
                    raise RuntimeError("RUNTIME_SCHEMA_LOCK_BUSY") from None
                self._file = handle
            self._lost = False
            self._acquired = True

    def ensure_owner(self):
        with self._guard:
            if not self._acquired or self._lost:
                raise RuntimeOwnershipLost()
            if self._connection is not None:
                try:
                    with self._connection.cursor() as cursor:
                        cursor.execute("SELECT IS_USED_LOCK(%s)", (self.name,))
                        if cursor.fetchone()[0] != self._connection_id:
                            self._lost = True
                except Exception:
                    self._lost = True
                if self._lost:
                    raise RuntimeOwnershipLost() from None
            elif self._file is None or self._file.closed:
                self._lost = True
                raise RuntimeOwnershipLost()

    def release(self):
        with self._guard:
            if self._connection is not None:
                try:
                    if not self._lost:
                        with self._connection.cursor() as cursor:
                            cursor.execute("SELECT RELEASE_LOCK(%s)", (self.name,))
                except Exception:
                    pass
                finally:
                    # 未知会话不可回池复用，避免持有未知的命名锁。
                    self._connection.invalidate()
                    self._connection.close()
                    self._connection = None
            if self._file is not None:
                self._file.close()
                self._file = None
            self._acquired = False
            self._lost = True


def _release_slot(session, run_id, owner_instance):
    session.execute(update(RuntimeControl).where(
        RuntimeControl.id == 1, RuntimeControl.active_job_id == run_id,
        RuntimeControl.owner_instance == owner_instance,
    ).values(active_job_type=None, active_job_id=None, owner_instance=None))


def recover_orphans(session_factory, owner_instance, ensure_owner):
    """调用者已取得schema锁后才允许识别孤立任务；从不提交新执行。"""
    ensure_owner()
    with session_factory() as session:
        with session.begin():
            slot = session.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())
            if slot is None:
                slot = RuntimeControl(id=1)
                session.add(slot)
            runs = list(session.scalars(select(EvaluationRun).where(EvaluationRun.status.in_(ACTIVE_STATUSES))))
            interrupted_runs = {run.id for run in runs}
            for run in runs:
                run.status, run.ended_at = "INTERRUPTED", utcnow()
                run.error = {"code": "PROCESS_INTERRUPTED", "message": "原执行进程已退出，未自动续跑"}
            score_sets = list(session.scalars(select(ScoreSet).where(
                (ScoreSet.status.in_(("QUEUED", "RUNNING"))) |
                ((ScoreSet.run_id.in_(interrupted_runs)) & (ScoreSet.status.in_(("PENDING", "FROZEN"))))
            )))
            for score_set in score_sets:
                score_set.status = "INTERRUPTED"
            for case in session.scalars(select(CaseRun).where(CaseRun.run_id.in_(interrupted_runs), CaseRun.phase != "EVALUATED")):
                # 已取得的回答/观测和单次Judge记录不被改写。
                case.phase = "INTERRUPTED"
            from .repeat_runs import recover_repeat_batches
            recover_repeat_batches(session, owner_instance)
            slot.active_job_type = slot.active_job_id = slot.owner_instance = None
            ensure_owner()
        return {"runs": len(runs), "score_sets": len(score_sets)}


class RuntimeManager:
    def __init__(self, engine, session_factory, *, worker=None, executor_factory=None,
                 on_ownership_lost=None, watchdog_interval_seconds=0.5):
        self.engine, self.session_factory = engine, session_factory
        self.owner_instance = str(uuid4())
        self.lease = SchemaLease(engine)
        self.worker = worker
        self.executor_factory = executor_factory or (lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="agenteval-run"))
        self.executor = None
        self._accept_guard = threading.RLock()
        self._stopping, self.started = False, False
        self.on_ownership_lost = on_ownership_lost
        self.watchdog_interval_seconds = watchdog_interval_seconds
        self.lost_event = threading.Event()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None

    def start(self):
        self.lease.acquire()
        try:
            self.recovery = recover_orphans(self.session_factory, self.owner_instance, self.lease.ensure_owner)
            self.executor = self.executor_factory()
            self.started = True
            self._watchdog_thread = threading.Thread(target=self._watch_ownership, daemon=True,
                                                     name="agenteval-schema-watchdog")
            self._watchdog_thread.start()
        except BaseException:
            self.lease.release()
            raise

    def _watch_ownership(self):
        while not self._watchdog_stop.wait(self.watchdog_interval_seconds):
            try:
                self.lease.ensure_owner()
            except RuntimeOwnershipLost:
                self._stopping = True
                self.lost_event.set()
                if self.on_ownership_lost is not None:
                    self.on_ownership_lost()
                return

    def ensure_owner(self):
        if not self.started or self._stopping:
            raise RuntimeOwnershipLost()
        self.lease.ensure_owner()

    def _fail_owned_run(self, run_id, status, code):
        with self.session_factory() as session:
            with session.begin():
                # 与接受/恢复统一slot→run顺序；条件必须在SQL里核对当前事实，
                # 不能用过期ORM对象的owner/status判断后无条件覆盖新进程收尾。
                _release_slot(session, run_id, self.owner_instance)
                changed = session.execute(update(EvaluationRun).where(
                    EvaluationRun.id == run_id, EvaluationRun.owner_instance == self.owner_instance,
                    EvaluationRun.status.in_(ACTIVE_STATUSES),
                ).values(status=status, ended_at=utcnow(), error={"code": code}))
                if changed.rowcount == 1:
                    session.execute(update(ScoreSet).where(
                        ScoreSet.run_id == run_id, ScoreSet.kind == "ORIGINAL",
                        ScoreSet.status.in_(("PENDING", "QUEUED", "RUNNING", "FROZEN")),
                    ).values(status=status))

    def _dispatch(self, run_id):
        try:
            self.ensure_owner()
            worker = self.worker
            if worker is None:
                from .runtime_worker import execute_run
                worker = execute_run
            worker(self.session_factory, run_id, self.owner_instance, ensure_owner=self.ensure_owner)
        except RuntimeOwnershipLost:
            self._fail_owned_run(run_id, "INTERRUPTED", "RUNTIME_OWNERSHIP_LOST")
        except Exception:
            self._fail_owned_run(run_id, "FAILED", "RUNTIME_WORKER_FAILED")

    def accept(self, request: StartRunRequest, *, experiment_id=None):
        if not self.started or self._stopping:
            raise ApiError(503, "RUNTIME_UNAVAILABLE", "后台执行器未启动")
        with self._accept_guard:
            self.ensure_owner()
            payload = request.model_dump(exclude={"request_id"})
            request_hash = digest(payload if experiment_id is None else payload | {"experiment_id": experiment_id})
            with self.session_factory() as session:
                with session.begin():
                    slot = session.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())
                    if slot is None:
                        raise ApiError(503, "RUNTIME_SLOT_MISSING", "活动槽缺失，请核对数据库迁移")
                    existing = session.scalar(select(EvaluationRun).where(EvaluationRun.request_id == request.request_id))
                    if existing is not None:
                        if existing.request_sha256 != request_hash:
                            raise ApiError(409, "REQUEST_ID_PAYLOAD_MISMATCH", "同一请求号不能使用不同启动参数")
                        return {"id": existing.id, "run_id": existing.id, "status": existing.status,
                                "total_cases": existing.total_cases, "idempotent_replay": True}
                    if slot.active_job_id:
                        field = "active_score_set_id" if slot.active_job_type == "RESCORE" else "active_run_id"
                        raise ApiError(409, "RUNTIME_BUSY", "已有活动任务，请查询其进度", [{"field": field, "value": slot.active_job_id}])
                    config = session.get(AgentConfig, request.agent_config_id)
                    judge = request.judge_profile
                    real_judge = judge.base_url is not None and getattr(judge, "source_type", "REAL_EXTERNAL") == "REAL_EXTERNAL"
                    if (config is not None and config.source_type == "REAL_EXTERNAL") or real_judge:
                        budget = request.execution_budget
                        if budget.total_timeout_seconds is None or budget.max_total_calls is None:
                            raise ApiError(422, "REAL_EXECUTION_BUDGET_REQUIRED", "真实外部执行须明确总时限和总调用预算", [{"field": "execution_budget", "code": "explicit_total_budget_required"}])
                    frozen = FreezeRequest.model_validate(payload)
                    run = freeze_run(session, frozen, request_id=request.request_id, request_sha256=request_hash,
                                     status="QUEUED", owner_instance=self.owner_instance, experiment_id=experiment_id)
                    if run.total_cases > 1000:
                        raise ApiError(422, "RUNTIME_CASE_LIMIT", "单批最多接受1000条启用用例")
                    slot.active_job_type, slot.active_job_id, slot.owner_instance = "RUN", run.id, self.owner_instance
                    self.ensure_owner()
                    accepted = {"id": run.id, "run_id": run.id, "status": "QUEUED", "total_cases": run.total_cases,
                                "snapshot_sha256": run.snapshot_sha256, "idempotent_replay": False}
                # 到这里计划、ORIGINAL评分集和所有PENDING用例已提交。
            try:
                self.executor.submit(self._dispatch, accepted["run_id"])
            except Exception:
                self._fail_owned_run(accepted["run_id"], "FAILED", "RUNTIME_REGISTRATION_FAILED")
                raise ApiError(503, "RUNTIME_REGISTRATION_FAILED", "后台任务注册失败，已保存失败记录", [{"field": "run_id", "value": accepted["run_id"]}]) from None
            return accepted

    def close(self):
        with self._accept_guard:
            self._stopping = True
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=2)
        try:
            if self.executor is not None:
                # 正在进行的HTTP有既定绝对截止；持锁直到当前工作停止。
                self.executor.shutdown(wait=True, cancel_futures=True)
            if self.started:
                with self.session_factory() as session:
                    active = list(session.scalars(select(EvaluationRun.id).where(EvaluationRun.owner_instance == self.owner_instance, EvaluationRun.status.in_(ACTIVE_STATUSES))))
                for run_id in active:
                    self._fail_owned_run(run_id, "INTERRUPTED", "APPLICATION_SHUTDOWN")
                with self.session_factory() as session:
                    slot = session.get(RuntimeControl, 1)
                    rescore_id = slot.active_job_id if slot and slot.owner_instance == self.owner_instance and slot.active_job_type == "RESCORE" else None
                if rescore_id:
                    from .rescore_worker import finish_rescore
                    finish_rescore(self.session_factory, rescore_id, self.owner_instance, "INTERRUPTED")
                with self.session_factory() as session:
                    slot = session.get(RuntimeControl, 1)
                    repeat_id = slot.active_job_id if slot and slot.owner_instance == self.owner_instance and slot.active_job_type == "REPEAT" else None
                if repeat_id:
                    from .repeat_runs import finish_repeat
                    finish_repeat(self.session_factory, repeat_id, self.owner_instance, "INTERRUPTED", "APPLICATION_SHUTDOWN")
        finally:
            self.started = False
            self.lease.release()

    def _dispatch_rescore(self, score_id):
        from .rescore_worker import execute_rescore, finish_rescore
        try:
            self.ensure_owner()
            execute_rescore(self.session_factory, score_id, self.owner_instance, ensure_owner=self.ensure_owner)
        except RuntimeOwnershipLost:
            finish_rescore(self.session_factory, score_id, self.owner_instance, "INTERRUPTED")
        except Exception:
            finish_rescore(self.session_factory, score_id, self.owner_instance, "FAILED")

    def accept_rescore(self, request):
        from .experiment_schemas import RescoreRequest
        from .privacy import known_secrets, sanitize
        from .scoring_api import _history, _score_set
        from .schemas import JudgeProfile, RuleProfile
        from .rescore_worker import finish_rescore

        request = RescoreRequest.model_validate(request)
        if not self.started or self._stopping:
            raise ApiError(503, "RUNTIME_UNAVAILABLE", "后台执行器未启动")
        with self._accept_guard:
            self.ensure_owner()
            with self.session_factory() as session, session.begin():
                slot = session.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())
                if slot is None:
                    raise ApiError(503, "RUNTIME_SLOT_MISSING", "活动槽缺失，请核对数据库迁移")
                if slot.active_job_id:
                    raise ApiError(409, "RUNTIME_BUSY", "已有活动任务，请查询其进度", [{"field": "active_job_id", "value": slot.active_job_id}])
                run = _history(session, EvaluationRun, request.source_run_id)
                if run.status in ACTIVE_STATUSES:
                    raise ApiError(409, "RUN_STILL_ACTIVE", "运行尚未结束，不能重新评分")
                parent = _score_set(session, run.id, request.parent_score_set_id)
                if session.scalar(select(CaseRun.id).where(CaseRun.run_id == run.id, CaseRun.agent_status.is_not(None))) is None:
                    raise ApiError(422, "NO_SAVED_EXECUTION", "此运行没有已保存的 Agent 执行，不能重新评分")
                judge = request.judge_profile or JudgeProfile.model_validate(dict(parent.judge_snapshot))
                rules = request.rule_profile or RuleProfile.model_validate(dict(parent.rule_snapshot))
                budget = run.snapshot["execution_budget"]
                if judge.base_url and judge.source_type == "REAL_EXTERNAL" and (budget.get("total_timeout_seconds") is None or budget.get("max_total_calls") is None):
                    raise ApiError(422, "REAL_EXECUTION_BUDGET_REQUIRED", "重评分使用原运行预算，真实 Judge 须有明确总时限和调用预算")
                judge_snapshot = judge.model_dump()
                ref = judge_snapshot.pop("credential_ref")
                secrets = known_secrets({"judge_ref": ref})
                judge_snapshot = sanitize(judge_snapshot, secrets) | {"credential_ref": ref}
                rule_snapshot = sanitize(rules.model_dump(), secrets)
                score = ScoreSet(run_id=run.id, parent_score_set_id=parent.id, kind="RESCORE",
                                 judge_snapshot=judge_snapshot, rule_snapshot=rule_snapshot,
                                 fingerprint=digest({"judge": judge_snapshot, "rules": rule_snapshot}), status="QUEUED")
                session.add(score)
                session.flush()
                slot.active_job_type, slot.active_job_id, slot.owner_instance = "RESCORE", score.id, self.owner_instance
                self.ensure_owner()
                accepted = {"id": score.id, "run_id": run.id, "kind": "RESCORE", "status": "QUEUED"}
            try:
                self.executor.submit(self._dispatch_rescore, accepted["id"])
            except Exception:
                finish_rescore(self.session_factory, accepted["id"], self.owner_instance, "FAILED")
                raise ApiError(503, "RUNTIME_REGISTRATION_FAILED", "重评分注册失败，已保存失败记录", [{"field": "score_set_id", "value": accepted["id"]}]) from None
            return accepted
