"""只读原始 Agent 执行并追加评分；绝不构建适配器或重放 Agent。"""

import time

from sqlalchemy import select, update

from .evaluation_models import CaseAssessment, JudgeResult, MetricResult
from .evaluation_store import append_judge_attempt, save_assessment
from .models import ExecutionResult
from .persistence import CaseRun, EvaluationRun, RuntimeControl, ScoreSet
from .rules import combine_metrics, evaluate_rules
from .runtime_adapters import RequestGuard, RuntimeBudgetExceeded, RuntimeOwnershipLostGuard, build_judge
from .schemas import DEFAULT_REQUIRED, METRICS


class _StorageFailure(RuntimeError):
    pass


def finish_rescore(factory, score_id, owner, status):
    with factory() as session, session.begin():
        slot = session.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())
        if slot is None or slot.active_job_id != score_id or slot.owner_instance != owner or slot.active_job_type != "RESCORE":
            return
        session.execute(update(ScoreSet).where(ScoreSet.id == score_id, ScoreSet.status.in_(("QUEUED", "RUNNING"))).values(status=status))
        slot.active_job_type = slot.active_job_id = slot.owner_instance = None


def _write(factory, score_id, owner, action):
    try:
        with factory() as session, session.begin():
            slot = session.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())
            score = session.get(ScoreSet, score_id)
            if (slot is None or slot.active_job_id != score_id or slot.owner_instance != owner
                    or slot.active_job_type != "RESCORE" or score is None or score.status != "RUNNING"):
                raise RuntimeOwnershipLostGuard("RESCORE_NOT_OWNED")
            return action(session, score)
    except RuntimeOwnershipLostGuard:
        raise
    except Exception as exc:
        raise _StorageFailure("RESCORE_PERSISTENCE_FAILED") from exc


def execute_rescore(session_factory, score_id, owner_instance, *, ensure_owner, judge_factory=None):
    judge_factory = judge_factory or build_judge
    status, claimed = "COMPLETED", False
    try:
        try:
            ensure_owner()
        except Exception as exc:
            raise RuntimeOwnershipLostGuard("RUNTIME_OWNERSHIP_LOST") from exc
        with session_factory() as session, session.begin():
            slot = session.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())
            score = session.get(ScoreSet, score_id)
            if (slot is None or slot.active_job_type != "RESCORE" or slot.active_job_id != score_id
                    or slot.owner_instance != owner_instance or score is None or score.status != "QUEUED"):
                return
            claimed, score.status = True, "RUNNING"
            run = session.get(EvaluationRun, score.run_id)
            run_id = run.id
            snapshot = dict(run.snapshot) | {"judge": dict(score.judge_snapshot), "rules": dict(score.rule_snapshot)}
            cases = [(row.id, row.case_snapshot, (row.raw_agent or {}).get("normalized_execution"))
                     for row in session.scalars(select(CaseRun).where(CaseRun.run_id == run_id).order_by(CaseRun.case_key))]
        budget = snapshot["execution_budget"]
        guard = RequestGuard(ensure_owner, budget, deadline=time.monotonic() +
                             (budget.get("total_timeout_seconds") or len(cases) * budget["case_timeout_seconds"]))
        for case_id, content, raw in cases:
            guard.case_deadline = None
            guard.check()
            guard.case_deadline = min(guard.deadline, time.monotonic() + budget["case_timeout_seconds"])
            result = ExecutionResult.model_validate(raw) if raw is not None else None

            def save_attempt(attempt):
                _write(session_factory, score_id, owner_instance,
                       lambda session, score: append_judge_attempt(session, run_id, score_id, case_id, attempt))

            if result is None:
                required = set(content.get("required_metrics", DEFAULT_REQUIRED))
                assessment = CaseAssessment(metrics=[MetricResult(metric=metric, status="NA", required=metric in required,
                    reason="ORIGINAL_EXECUTION_UNAVAILABLE", evidence={"reused_execution": True}) for metric in sorted(METRICS)],
                    conclusion="UNDETERMINED", judge_status="NA")
            else:
                if result.answer is None:
                    judged = JudgeResult(status="NA", error_code="QUALITY_INPUT_UNAVAILABLE")
                else:
                    try:
                        guard.check()
                        judged = judge_factory(snapshot, budget, guard).evaluate(content, result, deadline=guard.case_deadline, on_attempt=save_attempt)
                    except (RuntimeOwnershipLostGuard, _StorageFailure):
                        raise
                    except RuntimeBudgetExceeded as exc:
                        if exc.code.startswith("RUN_"):
                            raise
                        judged = JudgeResult(status="ERROR", error_code=exc.code)
                    except Exception:
                        judged = JudgeResult(status="ERROR", error_code="JUDGE_EXECUTION_ERROR")
                assessment = combine_metrics(content, snapshot["rules"], evaluate_rules(content, result, snapshot["rules"]), judged)

            def save(session, score):
                case = session.get(CaseRun, case_id)
                original_phase = case.phase
                save_assessment(session, run_id, score_id, case_id, assessment)
                # save_assessment 兼容原运行的 phase 更新；重评分在同一短事务中还原，
                # 对其他查询始终只暴露原执行事实，不修改原运行或其他评分集。
                case.phase = original_phase
                session.flush()

            _write(session_factory, score_id, owner_instance, save)
    except RuntimeOwnershipLostGuard:
        status = "INTERRUPTED"
    except Exception:
        status = "FAILED"
    finally:
        if claimed:
            finish_rescore(session_factory, score_id, owner_instance, status)
