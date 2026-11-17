import os
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4

import pytest
from sqlalchemy import select

from agenteval.db import Base, make_engine, session_factory
from agenteval.persistence import AgentConfig, CaseRun, Dataset, EvaluationRun, RuntimeControl, ScoreSet
from agenteval.runtime_control import RuntimeManager, RuntimeOwnershipLost, SchemaLease
from agenteval.schemas import CaseCreate, FreezeRequest
from agenteval.services import write_case
from agenteval.snapshots import freeze_run


class HoldingExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def shutdown(self, **kwargs):
        pass


def populate(factory):
    with factory.begin() as session:
        dataset = Dataset(name="运行测试")
        config = AgentConfig(name="模拟", adapter_type="MOCK", base_url="http://127.0.0.1", source_type="MOCK")
        session.add_all([dataset, config])
        session.flush()
        write_case(session, dataset, CaseCreate(case_key="A", question="问题", expected_answer="预期行为"))
        write_case(session, dataset, CaseCreate(case_key="B", question="问题2", expected_answer="预期行为2"))
        return dataset.id, config.id


@pytest.fixture
def runtime_db(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    yield engine, factory
    engine.dispose()


def test_file_schema_lock_refuses_second_process_before_recovery(runtime_db):
    engine, factory = runtime_db
    lease = SchemaLease(engine)
    lease.acquire()
    script = """
import sys
from agenteval.db import make_engine
from agenteval.runtime_control import SchemaLease
lease = SchemaLease(make_engine(sys.argv[1]))
try:
    lease.acquire()
except RuntimeError as exc:
    print(str(exc))
    raise SystemExit(31)
raise SystemExit(0)
"""
    try:
        result = subprocess.run([sys.executable, "-c", script, str(engine.url)], capture_output=True, text=True, timeout=15)
        assert result.returncode == 31, result.stderr
        assert result.stdout.strip() == "RUNTIME_SCHEMA_LOCK_BUSY"
    finally:
        lease.release()
    other = SchemaLease(engine)
    other.acquire()
    other.ensure_owner()
    other.release()


def test_schema_lease_loss_never_reacquires(runtime_db):
    engine, _ = runtime_db
    lease = SchemaLease(engine)
    lease.acquire()
    lease._file.close()
    for _ in range(2):
        with pytest.raises(RuntimeOwnershipLost):
            lease.ensure_owner()
    lease.release()


def test_idle_watchdog_detects_loss_and_signals_shutdown(runtime_db):
    engine, factory = runtime_db
    stop_requested = threading.Event()
    manager = RuntimeManager(engine, factory, executor_factory=HoldingExecutor,
                             on_ownership_lost=stop_requested.set, watchdog_interval_seconds=0.01)
    manager.start()
    try:
        manager.lease._file.close()
        assert stop_requested.wait(2)
        assert manager.lost_event.is_set()
        with pytest.raises(RuntimeOwnershipLost):
            manager.ensure_owner()
        assert manager.executor.jobs == []
    finally:
        manager.close()


def test_only_after_lock_recover_run_rescore_keep_answer_and_fixed_n(runtime_db):
    engine, factory = runtime_db
    dataset_id, config_id = populate(factory)
    with factory.begin() as session:
        run = freeze_run(session, FreezeRequest(dataset_id=dataset_id, agent_config_id=config_id),
                         status="RUNNING", owner_instance="dead-owner")
        original = session.scalar(select(ScoreSet).where(ScoreSet.run_id == run.id))
        original.status = "RUNNING"
        rescore = ScoreSet(run_id=run.id, parent_score_set_id=original.id, kind="RESCORE",
                           judge_snapshot=dict(original.judge_snapshot), rule_snapshot=dict(original.rule_snapshot),
                           fingerprint=original.fingerprint, status="QUEUED")
        session.add(rescore)
        case = session.scalar(select(CaseRun).where(CaseRun.run_id == run.id))
        case.answer, case.agent_status, case.phase = "退出前已保存回答", "SUCCEEDED", "AGENT_SAVED"
        session.add(RuntimeControl(id=1, active_job_type="RUN", active_job_id=run.id, owner_instance="dead-owner"))
        run_id = run.id
    lock = SchemaLease(engine)
    lock.acquire()
    blocked = RuntimeManager(engine, factory, executor_factory=HoldingExecutor)
    try:
        with pytest.raises(RuntimeError, match="RUNTIME_SCHEMA_LOCK_BUSY"):
            blocked.start()
        with factory() as session:
            assert session.get(EvaluationRun, run_id).status == "RUNNING"
    finally:
        lock.release()
    manager = RuntimeManager(engine, factory, executor_factory=HoldingExecutor)
    manager.start()
    try:
        assert manager.recovery == {"runs": 1, "score_sets": 2}
        assert manager.executor.jobs == []
        with factory() as session:
            run = session.get(EvaluationRun, run_id)
            assert run.status == "INTERRUPTED" and run.total_cases == 2
            assert {row.status for row in session.scalars(select(ScoreSet))} == {"INTERRUPTED"}
            cases = list(session.scalars(select(CaseRun).where(CaseRun.run_id == run.id)))
            assert len(cases) == 2 and cases[0].answer == "退出前已保存回答"
            assert session.get(RuntimeControl, 1).active_job_id is None
    finally:
        manager.close()


def test_shutdown_keeps_frozen_plan_and_does_not_replay(runtime_db):
    from agenteval.runtime_schemas import StartRunRequest
    engine, factory = runtime_db
    dataset_id, config_id = populate(factory)
    manager = RuntimeManager(engine, factory, executor_factory=HoldingExecutor)
    manager.start()
    result = manager.accept(StartRunRequest(request_id=str(uuid4()), dataset_id=dataset_id, agent_config_id=config_id))
    assert len(manager.executor.jobs) == 1
    manager.close()
    with factory() as session:
        assert session.get(EvaluationRun, result["run_id"]).status == "INTERRUPTED"
        assert len(list(session.scalars(select(CaseRun)))) == 2
        assert session.get(RuntimeControl, 1).active_job_id is None


def test_late_failure_cannot_overwrite_recovered_run_or_new_owner_slot(runtime_db):
    engine, factory = runtime_db
    dataset_id, config_id = populate(factory)
    manager = RuntimeManager(engine, factory, executor_factory=HoldingExecutor)
    with factory.begin() as session:
        run = freeze_run(session, FreezeRequest(dataset_id=dataset_id, agent_config_id=config_id),
                         status="RUNNING", owner_instance=manager.owner_instance)
        score = session.scalar(select(ScoreSet).where(ScoreSet.run_id == run.id))
        score.status = "RUNNING"
        session.add(RuntimeControl(id=1, active_job_type="RUN", active_job_id=run.id,
                                   owner_instance=manager.owner_instance))
        run_id = run.id
    stale = factory()
    # 保留旧身份映射，确定性模拟旧线程读到RUNNING后新进程先恢复并接受新任务。
    with stale.begin():
        old_run = stale.get(EvaluationRun, run_id)
        old_score = stale.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id))
        old_slot = stale.get(RuntimeControl, 1)
    new_run_id, new_owner = str(uuid4()), "new-owner-after-recovery"
    with factory.begin() as session:
        session.get(EvaluationRun, run_id).status = "INTERRUPTED"
        session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id)).status = "INTERRUPTED"
        slot = session.get(RuntimeControl, 1)
        slot.active_job_id, slot.owner_instance = new_run_id, new_owner
    assert old_run.status == old_score.status == "RUNNING"
    assert old_slot.owner_instance == manager.owner_instance
    manager.session_factory = lambda: stale
    manager._fail_owned_run(run_id, "FAILED", "LATE_OLD_WORKER_FAILURE")
    with factory() as session:
        assert session.get(EvaluationRun, run_id).status == "INTERRUPTED"
        assert session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id)).status == "INTERRUPTED"
        slot = session.get(RuntimeControl, 1)
        assert (slot.active_job_id, slot.owner_instance) == (new_run_id, new_owner)
