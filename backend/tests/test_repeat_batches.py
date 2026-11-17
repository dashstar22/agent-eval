from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from agenteval.db import Base, make_engine, session_factory
from agenteval.main import create_app
from agenteval.persistence import CaseRun, EvaluationRun, RuntimeControl, ScoreSet
from agenteval.repeat_models import RepeatBatch
from agenteval.repeat_runs import dispatch_repeat
from agenteval.runtime_control import RuntimeOwnershipLost
from test_runtime_control import HoldingExecutor
from test_workbench import setup_inputs, freeze, record


@pytest.fixture
def app_data(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'repeat.sqlite'}")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    app = create_app(engine, runtime_executor_factory=HoldingExecutor)
    with TestClient(app, raise_server_exceptions=False) as client:
        inputs = setup_inputs(client)
        source = freeze(client, inputs)
        record(factory, source, "A", 5)
        record(factory, source, "B", 5)
        yield client, app, factory, source
    engine.dispose()


def request(source, **changes):
    return {"request_id": str(uuid4()), "source_run_id": source, "repeat_count": 2} | changes


def test_repeat_clones_frozen_input_and_is_idempotent_then_executes_sequentially(app_data):
    client, app, factory, source = app_data
    payload = request(source)
    created = client.post('/api/v1/extensions/repeat-batches', json=payload)
    assert created.status_code == 202, created.text
    batch = created.json()
    assert len(batch['runs']) == 2 and batch['status'] == 'QUEUED'
    assert batch['budget']['real_execution'] is False
    assert client.post('/api/v1/extensions/repeat-batches', json=payload).json()['idempotent_replay'] is True
    assert len(app.state.runtime_manager.executor.jobs) == 1
    assert client.post('/api/v1/extensions/repeat-batches', json=payload | {'repeat_count': 3}).status_code == 409
    assert client.post('/api/v1/extensions/repeat-batches', json=request(source)).json()['code'] == 'RUNTIME_BUSY'
    with factory.begin() as session:
        original = session.get(EvaluationRun, source)
        for item in batch['runs']:
            clone = session.get(EvaluationRun, item['id'])
            assert clone.snapshot == original.snapshot
            assert clone.snapshot_sha256 == original.snapshot_sha256
            assert clone.id != original.id
        original_answers = [row.answer for row in session.scalars(select(CaseRun).where(CaseRun.run_id == source))]
    job, args = app.state.runtime_manager.executor.jobs.pop()
    job(*args)
    finished = client.get('/api/v1/extensions/repeat-batches/' + batch['id']).json()
    assert finished['status'] == 'COMPLETED'
    assert all(item['status'] == 'COMPLETED' for item in finished['runs'])
    assert len({item['score_set_id'] for item in finished['runs']}) == 2
    with factory() as session:
        assert session.get(RuntimeControl, 1).active_job_id is None
        assert [row.answer for row in session.scalars(select(CaseRun).where(CaseRun.run_id == source))] == original_answers
        assert all(session.scalar(select(CaseRun.answer).where(CaseRun.run_id == item['id'])) for item in finished['runs'])


def test_failed_member_stops_remaining_and_retains_history(app_data):
    client, app, factory, source = app_data
    def failing(*args, **kwargs):
        raise RuntimeError('Synthetic execution failure')
    app.state.runtime_manager.worker = failing
    batch = client.post('/api/v1/extensions/repeat-batches', json=request(source)).json()
    job, args = app.state.runtime_manager.executor.jobs.pop()
    job(*args)
    finished = client.get('/api/v1/extensions/repeat-batches/' + batch['id']).json()
    assert finished['status'] == 'FAILED'
    assert [run['status'] for run in finished['runs']] == ['FAILED', 'INTERRUPTED']
    with factory() as session:
        assert session.get(RuntimeControl, 1).active_job_id is None
        assert session.get(EvaluationRun, source).status == 'COMPLETED'


def test_shutdown_queued_batch_marks_members_and_batch_interrupted(app_data):
    client, app, factory, source = app_data
    batch = client.post('/api/v1/extensions/repeat-batches', json=request(source)).json()
    app.state.runtime_manager.close()
    with factory() as session:
        assert session.get(RepeatBatch, batch['id']).status == 'INTERRUPTED'
        assert session.get(RuntimeControl, 1).active_job_id is None
        assert all(session.get(EvaluationRun, row['id']).status == 'INTERRUPTED' for row in batch['runs'])


def test_real_execution_requires_explicit_confirmation_and_source_budget(app_data):
    client, app, factory, source = app_data
    with factory.begin() as session:
        original = session.get(EvaluationRun, source)
        snapshot = dict(original.snapshot)
        # 测试用独立来源行，不修改原不可变快照。
        snapshot['agent'] = dict(snapshot['agent']) | {'source_type': 'REAL_EXTERNAL'}
        snapshot['execution_budget'] = dict(snapshot['execution_budget']) | {'total_timeout_seconds': 30, 'max_total_calls': 6}
        real = EvaluationRun(experiment_id=original.experiment_id, request_id=str(uuid4()),
            snapshot=snapshot, snapshot_sha256='a' * 64, status='COMPLETED', total_cases=2)
        session.add(real)
        session.flush()
        real_id = real.id
    rejected = client.post('/api/v1/extensions/repeat-batches', json=request(real_id))
    assert rejected.status_code == 422 and rejected.json()['code'] == 'REAL_REPEAT_CONFIRMATION_REQUIRED'
    accepted = client.post('/api/v1/extensions/repeat-batches', json=request(real_id, confirm_real_execution=True)).json()
    assert accepted['budget']['total_max_calls'] == 12
    assert accepted['budget']['total_timeout_seconds'] == 60
    # HoldingExecutor 不执行，因此该确认测试绝不发出真实请求。


def test_repeat_rejects_unfinished_source_and_invalid_counts(app_data):
    client, app, factory, source = app_data
    with factory.begin() as session:
        session.get(EvaluationRun, source).status = 'INTERRUPTED'
    assert client.post('/api/v1/extensions/repeat-batches', json=request(source)).json()['code'] == 'SOURCE_RUN_NOT_COMPLETED'
    assert client.post('/api/v1/extensions/repeat-batches', json=request(source, repeat_count=6)).status_code == 422


def test_batch_claim_checks_lease_before_commit_and_old_dispatch_preserves_recovery(app_data, monkeypatch):
    client, app, factory, source = app_data
    manager = app.state.runtime_manager
    batch = client.post('/api/v1/extensions/repeat-batches', json=request(source)).json()
    ensure = manager.ensure_owner
    calls = 0
    def lose_before_claim_commit():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeOwnershipLost()
        ensure()
    monkeypatch.setattr(manager, 'ensure_owner', lose_before_claim_commit)
    dispatch_repeat(manager, batch['id'])
    with factory.begin() as session:
        saved = session.get(RepeatBatch, batch['id'])
        assert saved.started_at is None  # 领取事务因提交前丢锁而回滚。
        saved.error = {'code': 'PROCESS_INTERRUPTED', 'message': '新所有者已恢复'}
    dispatch_repeat(manager, batch['id'])
    with factory() as session:
        assert session.get(RepeatBatch, batch['id']).error['code'] == 'PROCESS_INTERRUPTED'
        assert all(session.scalar(select(CaseRun.agent_status).where(CaseRun.run_id == row['id'])) is None for row in batch['runs'])
