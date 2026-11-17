"""单线程批次执行；所有外部等待均位于数据库事务之外。"""

import time
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from .adapters.printer import PersistenceFailure
from .db import utcnow
from .evaluation_models import JudgeResult
from .evaluation_store import append_judge_attempt, save_assessment, save_execution
from .models import ExecutionResult, now
from .persistence import CaseRun, EvaluationRun, RuntimeControl, ScoreSet
from .privacy import bounded_raw, known_secrets
from .rules import combine_metrics, evaluate_rules
from .runtime_adapters import (
    RequestGuard, RuntimeBudgetExceeded, RuntimeOwnershipLostGuard, build_adapter, build_judge,
)


class _StorageFailure(RuntimeError):
    pass


def _write(factory, run_id, owner, action):
    """持久化故障不能作为单题错误继续，避免已丢答案后继续产生调用。"""
    try:
        with factory() as session, session.begin():
            run = session.get(EvaluationRun, run_id)
            if run is None or run.owner_instance != owner or run.status != "RUNNING":
                raise RuntimeOwnershipLostGuard("RUNTIME_RUN_NOT_OWNED")
            return action(session, run)
    except RuntimeOwnershipLostGuard:
        raise
    except Exception as exc:
        raise _StorageFailure("RUNTIME_PERSISTENCE_FAILED") from exc


def _error_result(source_type, started, stamp, code, *, timeout=False):
    return ExecutionResult(
        execution_id=str(uuid4()), agent_status="TIMEOUT" if timeout else "FAILED",
        source_type=source_type, answer=None, started_at=stamp, ended_at=now(),
        duration_ms=max(0, round((time.monotonic() - started) * 1000)), error_code=code,
    )


def _finish(factory, run_id, owner, status, error=None):
    # 与接受/恢复保持 slot→run 锁顺序；owner/status必须成为UPDATE条件，
    # 不能依赖可能已过期的ORM对象检查后再用主键无条件写回。
    with factory() as session, session.begin():
        session.execute(update(RuntimeControl).where(
            RuntimeControl.id == 1, RuntimeControl.active_job_id == run_id,
            RuntimeControl.owner_instance == owner,
        ).values(active_job_id=None, active_job_type=None, owner_instance=None))
        changed = session.execute(update(EvaluationRun).where(
            EvaluationRun.id == run_id, EvaluationRun.owner_instance == owner,
            EvaluationRun.status.in_(("QUEUED", "RUNNING")),
        ).values(status=status, ended_at=utcnow(), error={"code": error} if error else None))
        if changed.rowcount == 1:
            session.execute(update(ScoreSet).where(
                ScoreSet.run_id == run_id, ScoreSet.kind == "ORIGINAL",
                ScoreSet.status.in_(("PENDING", "QUEUED", "RUNNING", "FROZEN")),
            ).values(status=status))


def execute_run(session_factory, run_id, owner_instance, *, ensure_owner,
                adapter_factory=None, judge_factory=None):
    """执行已接受计划。额外 factory 仅供受控测试，不改变生产 HTTP 契约。"""
    adapter_factory = adapter_factory or build_adapter
    judge_factory = judge_factory or build_judge
    terminal, error = "COMPLETED", None
    claimed = False
    try:
        try:
            ensure_owner()
        except Exception as exc:
            raise RuntimeOwnershipLostGuard("RUNTIME_OWNERSHIP_LOST") from exc
        with session_factory() as session, session.begin():
            run = session.scalar(select(EvaluationRun).where(EvaluationRun.id == run_id).with_for_update())
            if run is None or run.owner_instance != owner_instance or run.status != "QUEUED":
                return
            claimed = True
            run.status, run.started_at = "RUNNING", utcnow()
            snapshot = run.snapshot
            cases = [(case.id, case.case_snapshot) for case in session.scalars(
                select(CaseRun).where(CaseRun.run_id == run_id).order_by(CaseRun.case_key))]
            score = session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id, ScoreSet.kind == "ORIGINAL"))
            if score is None:
                raise _StorageFailure("ORIGINAL_SCORE_SET_MISSING")
            score_id, score.status = score.id, "RUNNING"
        budget = snapshot["execution_budget"]
        started = time.monotonic()
        total_seconds = budget.get("total_timeout_seconds")
        # 未设置总预算的开发 Mock 仍有按题数推导的有限墙钟边界。
        total_seconds = total_seconds or len(cases) * budget["case_timeout_seconds"]
        guard = RequestGuard(ensure_owner, budget, deadline=started + total_seconds)
        for case_id, content in cases:
            guard.case_deadline = None
            guard.check()
            case_started, stamp = time.monotonic(), now()
            guard.case_deadline = min(guard.deadline, case_started + budget["case_timeout_seconds"])
            guard.agent_calls = 0
            _write(session_factory, run_id, owner_instance,
                   lambda session, run: setattr(session.get(CaseRun, case_id), "phase", "EXECUTING"))
            last_saved = None
            prior_saved = []
            previous = content.get("preconditions", {}).get("prior_turns", [])
            adapter_type = snapshot["agent"]["adapter_type"]

            def save_answer(result):
                nonlocal last_saved
                # 原适配器会回调每个显式前置轮次；这些不是目标评分执行身份。
                if adapter_type == "PRINTER_HTTP" and len(prior_saved) < len(previous):
                    prior_saved.append(result.model_dump(mode="json"))
                    def save_prior(session, run):
                        row = session.get(CaseRun, case_id)
                        row.raw_agent = {"prerequisite_progress": bounded_raw(prior_saved, known_secrets())}
                        row.phase = "PREREQUISITE_SAVED"
                    _write(session_factory, run_id, owner_instance, save_prior)
                    return
                _write(session_factory, run_id, owner_instance,
                       lambda session, run: save_execution(session, case_id, result))
                last_saved = result

            try:
                adapter = adapter_factory(snapshot, budget, guard)
                kwargs = {"preconditions": previous}
                if adapter_type == "PRINTER_HTTP":
                    order_id = content.get("preconditions", {}).get("order_id")
                    if type(order_id) is not int or order_id <= 0:
                        raise ValueError("PRINTER_ORDER_ID_REQUIRED")
                    kwargs["order_id"] = order_id
                if adapter_type == "MOCK":
                    # Mock 无 HTTP，但仍受同一用例和所有权守卫。
                    guard.check()
                result = adapter.run(content["question"], save_answer, **kwargs)
                _write(session_factory, run_id, owner_instance,
                       lambda session, run: save_execution(session, case_id, result))
                last_saved = result
            except (RuntimeOwnershipLostGuard, _StorageFailure, SQLAlchemyError, PersistenceFailure):
                raise
            except RuntimeBudgetExceeded as exc:
                if exc.code.startswith("RUN_"):
                    raise
                if last_saved is None:
                    result = _error_result(snapshot["agent"]["source_type"], case_started, stamp,
                                           exc.code, timeout=exc.code == "CASE_DEADLINE_EXCEEDED")
                else:
                    # 观测截止不能擦掉已经成功提交的 Agent 回答。
                    result = last_saved.model_copy(update={"observation_status": "UNAVAILABLE", "observation_reason": exc.code})
                _write(session_factory, run_id, owner_instance,
                       lambda session, run: save_execution(session, case_id, result))
            except Exception as exc:
                code = "PRINTER_ORDER_ID_REQUIRED" if isinstance(exc, ValueError) and str(exc) == "PRINTER_ORDER_ID_REQUIRED" else "AGENT_EXECUTION_ERROR"
                result = (last_saved.model_copy(update={"observation_status": "UNAVAILABLE", "observation_reason": code})
                          if last_saved is not None else
                          _error_result(snapshot["agent"]["source_type"], case_started, stamp, code))
                _write(session_factory, run_id, owner_instance,
                       lambda session, run: save_execution(session, case_id, result))

            _write(session_factory, run_id, owner_instance,
                   lambda session, run: setattr(session.get(CaseRun, case_id), "phase", "SCORING"))

            def save_attempt(attempt):
                _write(session_factory, run_id, owner_instance,
                       lambda session, run: append_judge_attempt(session, run_id, score_id, case_id, attempt))

            if result.answer is None:
                judged = JudgeResult(status="NA", error_code="QUALITY_INPUT_UNAVAILABLE")
            elif time.monotonic() >= guard.case_deadline:
                judged = JudgeResult(status="ERROR", error_code="JUDGE_DEADLINE_EXCEEDED")
            else:
                try:
                    guard.check()
                    judged = judge_factory(snapshot, budget, guard).evaluate(
                        content, result, deadline=guard.case_deadline, on_attempt=save_attempt)
                except (RuntimeOwnershipLostGuard, _StorageFailure, SQLAlchemyError, PersistenceFailure):
                    raise
                except RuntimeBudgetExceeded as exc:
                    if exc.code.startswith("RUN_"):
                        raise
                    judged = JudgeResult(status="ERROR", error_code=exc.code)
                except Exception:
                    judged = JudgeResult(status="ERROR", error_code="JUDGE_EXECUTION_ERROR")
            assessment = combine_metrics(content, snapshot["rules"],
                                         evaluate_rules(content, result, snapshot["rules"]), judged)
            _write(session_factory, run_id, owner_instance,
                   lambda session, run: save_assessment(session, run_id, score_id, case_id, assessment))
    except RuntimeOwnershipLostGuard:
        terminal, error = "INTERRUPTED", "RUNTIME_OWNERSHIP_LOST"
    except RuntimeBudgetExceeded as exc:
        terminal, error = "FAILED", exc.code
    except Exception:
        terminal, error = "FAILED", "RUNTIME_INFRASTRUCTURE_ERROR"
    finally:
        # 连接全面中断时无法伪装持久收尾；旧活动记录由下次取得锁的进程识别中断。
        if claimed:
            _finish(session_factory, run_id, owner_instance, terminal, error)
