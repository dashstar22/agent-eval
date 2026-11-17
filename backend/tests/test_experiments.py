"""真实事务验证实验身份、冻结历史、重评分独立性；网络均为明确协议替身。"""

import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agenteval.db import Base, make_engine
from agenteval.evaluation_models import JudgeResult
from agenteval.evaluation_store import load_summary_cases, save_execution
from agenteval.main import create_app
from agenteval.models import ExecutionResult, now
from agenteval.persistence import CaseEvaluation, CaseRun, EvaluationRun, EvaluationScore, JudgeAttempt, RuntimeControl, ScoreSet
from agenteval.rescore_worker import execute_rescore
from agenteval.runtime_adapters import build_judge
from agenteval.runtime_control import RuntimeManager
from agenteval.statistics import summarize
from test_runtime_control import HoldingExecutor


@pytest.fixture
def experiment_api(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'experiments.db'}")
    Base.metadata.create_all(engine)
    app = create_app(engine, runtime_executor_factory=HoldingExecutor)
    with TestClient(app, raise_server_exceptions=False) as client:
        dataset = client.post('/api/v1/datasets', json={'name': '实验冻结输入'}).json()
        config = client.post('/api/v1/agent-configs', json={'name': '模拟来源', 'adapter_type': 'MOCK'}).json()
        for key in ('A', 'B'):
            assert client.post(f"/api/v1/datasets/{dataset['id']}/cases", json={
                'case_key': key, 'question': f'问题{key}', 'expected_answer': '参考行为'}).status_code == 201
        payload = {'name': '候选实验', 'dataset_id': dataset['id'], 'agent_config_id': config['id'],
                   'version_label': '候选V1', 'purpose': '验证历史不覆盖'}
        yield client, app, payload
    engine.dispose()


def dispatch(app):
    fn, args = app.state.runtime_manager.executor.jobs.pop(0)
    fn(*args)


def create_and_complete(client, app, payload):
    experiment = client.post('/api/v1/experiments', json=payload)
    assert experiment.status_code == 201, experiment.text
    experiment = experiment.json()
    response = client.post(f"/api/v1/experiments/{experiment['id']}/runs", json={'request_id': str(uuid4())})
    assert response.status_code == 202, response.text
    run_id = response.json()['run_id']
    dispatch(app)
    assert client.get(f'/api/v1/runs/{run_id}').json()['status'] == 'COMPLETED'
    return experiment, run_id


def test_same_experiment_repeated_runs_identity_and_frozen_metadata(experiment_api):
    client, app, payload = experiment_api
    experiment, run1 = create_and_complete(client, app, payload)
    old = client.get(f'/api/v1/runs/{run1}').json()
    assert client.patch(f"/api/v1/experiments/{experiment['id']}", json={'version_label': '候选V2', 'purpose': '后续目的'}).status_code == 200
    response = client.post(f"/api/v1/experiments/{experiment['id']}/runs", json={'request_id': str(uuid4())})
    assert response.status_code == 202, response.text
    run2 = response.json()['run_id']
    assert run1 != run2
    dispatch(app)
    history = client.get(f"/api/v1/experiments/{experiment['id']}/runs?page_size=1").json()
    assert history['total'] == 2 and len(history['items']) == 1
    assert history['items'][0]['source_type'] == 'MOCK'
    assert history['items'][0]['experiment_id'] == experiment['id']
    assert client.get(f'/api/v1/runs/{run1}').json()['snapshot'] == old['snapshot']
    assert old['snapshot']['experiment']['purpose'] == '验证历史不覆盖'
    assert client.get(f'/api/v1/runs/{run2}').json()['snapshot']['candidate_version_label'] == '候选V2'
    assert client.delete(f"/api/v1/experiments/{experiment['id']}").status_code == 204
    assert client.get(f"/api/v1/experiments/{experiment['id']}").status_code == 404
    assert client.get(f"/api/v1/experiments/{experiment['id']}/runs").json()['total'] == 2
    assert client.get(f'/api/v1/runs/{run1}').status_code == 200


def test_experiment_validation_and_start_idempotency(experiment_api):
    client, app, payload = experiment_api
    assert client.post('/api/v1/experiments', json=payload | {'name': '  '}).status_code == 422
    assert client.post('/api/v1/experiments', json=payload | {'dataset_id': 99999}).status_code == 404
    experiment = client.post('/api/v1/experiments', json=payload).json()
    body = {'request_id': str(uuid4())}
    path = f"/api/v1/experiments/{experiment['id']}/runs"
    first = client.post(path, json=body)
    replay = client.post(path, json=body)
    assert first.status_code == replay.status_code == 202
    assert first.json()['run_id'] == replay.json()['run_id']
    assert len(app.state.runtime_manager.executor.jobs) == 1
    assert client.post(path, json={'request_id': str(uuid4())}).json()['code'] == 'RUNTIME_BUSY'
    assert client.get('/api/v1/runs?page_size=101').status_code == 422


def test_rescore_reuses_execution_and_preserves_original_score_phase_and_usage(experiment_api, monkeypatch):
    client, app, payload = experiment_api
    monkeypatch.setenv('RESCORE_TEST_KEY', 'private-rescore-canary')
    _, run_id = create_and_complete(client, app, payload)
    factory = app.state.session_factory
    original = client.get(f'/api/v1/runs/{run_id}/score-sets').json()['items'][0]
    with factory.begin() as session:
        for case in session.scalars(select(CaseRun).where(CaseRun.run_id == run_id)):
            case.phase = 'INTERRUPTED'
    before_cases = client.get(f'/api/v1/runs/{run_id}/cases').json()
    before_scoring = [client.get(f"/api/v1/case-runs/{case['id']}/scoring").json() for case in before_cases['items']]
    response = client.post('/api/v1/score-sets', json={'source_run_id': run_id,
        'judge_profile': {'base_url': 'http://judge.test/v1', 'model': 'judge-double',
                          'credential_ref': 'RESCORE_TEST_KEY', 'source_type': 'PROTOCOL_DOUBLE'},
        'rule_profile': {'quality_thresholds': {'correctness': 5, 'relevance': 4, 'task_completion': 4}}})
    assert response.status_code == 202, response.text
    score_id = response.json()['id']
    assert client.get(f'/api/v1/score-sets/{score_id}').json()['progress']['evaluated'] == 0
    assert client.post('/api/v1/score-sets', json={'source_run_id': run_id}).json()['code'] == 'RUNTIME_BUSY'
    calls = []

    def judge_response(request):
        calls.append(str(request.url))
        with factory() as session:
            assert session.get(EvaluationRun, run_id).status == 'COMPLETED'
            assert all(case.phase == 'INTERRUPTED' for case in session.scalars(select(CaseRun)))
        content = {'scores': {name: {'score': 4, 'reason': '协议替身评分'} for name in ('correctness', 'relevance', 'task_completion')}}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(content)}}],
                                      'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}})

    app.state.runtime_manager.executor.jobs.clear()
    execute_rescore(factory, score_id, app.state.runtime_manager.owner_instance,
                    ensure_owner=app.state.runtime_manager.ensure_owner,
                    judge_factory=lambda snapshot, budget, guard: build_judge(snapshot, budget, guard,
                        transport=httpx.MockTransport(judge_response)))
    after = client.get(f'/api/v1/score-sets/{score_id}').json()
    assert after['status'] == 'COMPLETED' and after['progress']['evaluated'] == 2
    assert after['agent_usage_reused'] is True and len(calls) == 2
    assert client.get(f'/api/v1/runs/{run_id}/cases').json() == before_cases
    for case, before in zip(before_cases['items'], before_scoring):
        assert client.get(f"/api/v1/case-runs/{case['id']}/scoring").json() == before
        new_scoring = client.get(f"/api/v1/case-runs/{case['id']}/scoring?score_set_id={score_id}").json()
        assert new_scoring['evaluation']['conclusion'] == 'FAIL'
    with factory() as session:
        original_summary = summarize(['A', 'B'], load_summary_cases(session, run_id, original['id']))
        new_summary = summarize(['A', 'B'], load_summary_cases(session, run_id, score_id))
        assert original_summary['usage']['Agent'] == new_summary['usage']['Agent']
        assert session.scalar(select(func.count()).select_from(CaseRun)) == 2
        assert session.scalar(select(func.count()).select_from(CaseEvaluation)) == 4
        assert session.get(RuntimeControl, 1).active_job_id is None
    assert client.get(f'/api/v1/runs/{run_id}/score-sets').json()['total'] == 2


def test_rescore_parent_mismatch_invalid_uuid_and_real_budget_rejected(experiment_api):
    client, app, payload = experiment_api
    _, run1 = create_and_complete(client, app, payload)
    _, run2 = create_and_complete(client, app, payload)
    score2 = client.get(f'/api/v1/runs/{run2}/score-sets').json()['items'][0]['id']
    assert client.post('/api/v1/score-sets', json={'source_run_id': run1, 'parent_score_set_id': score2}).status_code == 404
    assert client.post('/api/v1/score-sets', json={'source_run_id': 'invalid'}).status_code == 422
    assert client.post('/api/v1/score-sets', json={'source_run_id': run1,
        'judge_profile': {'base_url': 'http://judge.test', 'model': 'real-claim'}}).json()['code'] == 'REAL_EXECUTION_BUDGET_REQUIRED'
    assert app.state.runtime_manager.executor.jobs == []


def test_rescore_registration_failure_releases_slot_and_preserves_run(experiment_api):
    client, app, payload = experiment_api
    _, run_id = create_and_complete(client, app, payload)
    def fail(*args):
        raise RuntimeError('private-registration-error')
    app.state.runtime_manager.executor.submit = fail
    result = client.post('/api/v1/score-sets', json={'source_run_id': run_id})
    assert result.status_code == 503 and 'private-registration-error' not in result.text
    score_id = result.json()['fields'][0]['value']
    assert client.get(f'/api/v1/score-sets/{score_id}').json()['status'] == 'FAILED'
    assert client.get(f'/api/v1/runs/{run_id}').json()['status'] == 'COMPLETED'
    with app.state.session_factory() as session:
        assert session.get(RuntimeControl, 1).active_job_id is None


def test_rescore_lock_loss_after_attempt_stops_retry_and_keeps_original_facts(experiment_api, monkeypatch):
    client, app, payload = experiment_api
    monkeypatch.setenv('RESCORE_TEST_KEY', 'private-rescore-canary')
    _, run_id = create_and_complete(client, app, payload)
    before = client.get(f'/api/v1/runs/{run_id}/cases').json()
    response = client.post('/api/v1/score-sets', json={'source_run_id': run_id,
        'judge_profile': {'base_url': 'http://judge.test', 'model': 'judge-double',
                          'credential_ref': 'RESCORE_TEST_KEY', 'source_type': 'PROTOCOL_DOUBLE'}})
    score_id = response.json()['id']
    app.state.runtime_manager.executor.jobs.clear()
    calls, lost = [], [False]
    def ensure_owner():
        if lost[0]:
            raise RuntimeError('lost lease')
    def response_handler(request):
        calls.append(str(request.url))
        lost[0] = True
        return httpx.Response(200, json={'choices': [{'message': {'content': 'invalid-json'}}]})
    execute_rescore(app.state.session_factory, score_id, app.state.runtime_manager.owner_instance,
        ensure_owner=ensure_owner, judge_factory=lambda snapshot, budget, guard: build_judge(
            snapshot, budget, guard, transport=httpx.MockTransport(response_handler)))
    assert len(calls) == 1
    assert client.get(f'/api/v1/score-sets/{score_id}').json()['status'] == 'INTERRUPTED'
    assert client.get(f'/api/v1/runs/{run_id}/cases').json() == before
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JudgeAttempt).where(JudgeAttempt.score_set_id == score_id)) == 1
        assert session.scalar(select(func.count()).select_from(CaseEvaluation).where(CaseEvaluation.score_set_id == score_id)) == 0


def test_rescore_error_isolated_and_multiple_sets_keep_parent_lineage(experiment_api):
    client, app, payload = experiment_api
    _, run_id = create_and_complete(client, app, payload)
    first = client.post('/api/v1/score-sets', json={'source_run_id': run_id}).json()['id']
    assert client.get(f'/api/v1/runs/{run_id}/scoring-summary?score_set_id={first}').json()['summary']['provisional'] is True
    app.state.runtime_manager.executor.jobs.clear()
    seen = []
    class FailingJudge:
        def evaluate(self, content, result, **kwargs):
            seen.append(content['case_key'])
            raise RuntimeError('private-judge-failure')
    execute_rescore(app.state.session_factory, first, app.state.runtime_manager.owner_instance,
                    ensure_owner=app.state.runtime_manager.ensure_owner, judge_factory=lambda *args: FailingJudge())
    assert seen == ['A', 'B']
    assert client.get(f'/api/v1/score-sets/{first}').json()['status'] == 'COMPLETED'
    second = client.post('/api/v1/score-sets', json={'source_run_id': run_id, 'parent_score_set_id': first}).json()['id']
    dispatch(app)
    assert client.get(f'/api/v1/runs/{run_id}/score-sets').json()['total'] == 3
    assert client.get(f'/api/v1/score-sets/{second}').json()['parent_score_set_id'] == first
    assert client.get(f'/api/v1/runs/{run_id}').json()['status'] == 'COMPLETED'


def test_shutdown_and_restart_interrupt_only_active_rescore(experiment_api):
    client, app, payload = experiment_api
    _, run_id = create_and_complete(client, app, payload)
    before = client.get(f'/api/v1/runs/{run_id}/cases').json()
    score_id = client.post('/api/v1/score-sets', json={'source_run_id': run_id}).json()['id']
    old = app.state.runtime_manager
    old.close()
    assert client.get(f'/api/v1/score-sets/{score_id}').json()['status'] == 'INTERRUPTED'
    assert client.get(f'/api/v1/runs/{run_id}').json()['status'] == 'COMPLETED'
    assert client.get(f'/api/v1/runs/{run_id}/cases').json() == before
    # 制造已退出进程遗留的重评分，不制造新的 Agent 或 Judge 调用。
    with app.state.session_factory.begin() as session:
        session.get(ScoreSet, score_id).status = 'RUNNING'
        slot = session.get(RuntimeControl, 1)
        slot.active_job_id, slot.active_job_type, slot.owner_instance = score_id, 'RESCORE', 'dead-owner'
    restarted = RuntimeManager(old.engine, app.state.session_factory, executor_factory=HoldingExecutor)
    restarted.start()
    try:
        assert restarted.recovery == {'runs': 0, 'score_sets': 1}
        assert restarted.executor.jobs == []
        assert client.get(f'/api/v1/score-sets/{score_id}').json()['status'] == 'INTERRUPTED'
        assert client.get(f'/api/v1/runs/{run_id}/cases').json() == before
    finally:
        restarted.close()


def test_rescore_plan_persistence_failure_submits_no_judge_or_task(experiment_api, monkeypatch):
    client, app, payload = experiment_api
    _, run_id = create_and_complete(client, app, payload)
    original_flush = Session.flush
    def fail_rescore_flush(session, *args, **kwargs):
        if any(isinstance(row, ScoreSet) and row.kind == 'RESCORE' for row in session.new):
            raise SQLAlchemyError('private-persistence-failure')
        return original_flush(session, *args, **kwargs)
    monkeypatch.setattr(Session, 'flush', fail_rescore_flush)
    result = client.post('/api/v1/score-sets', json={'source_run_id': run_id})
    assert result.status_code == 503 and result.json()['code'] == 'DATABASE_ERROR'
    assert 'private-persistence-failure' not in result.text
    assert app.state.runtime_manager.executor.jobs == []
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ScoreSet)) == 1
        assert session.get(RuntimeControl, 1).active_job_id is None


def test_active_rescore_and_run_share_same_persistent_slot(experiment_api):
    client, app, payload = experiment_api
    experiment, run_id = create_and_complete(client, app, payload)
    score_id = client.post('/api/v1/score-sets', json={'source_run_id': run_id}).json()['id']
    blocked = client.post(f"/api/v1/experiments/{experiment['id']}/runs", json={'request_id': str(uuid4())})
    assert blocked.status_code == 409 and blocked.json()['code'] == 'RUNTIME_BUSY'
    assert blocked.json()['fields'] == [{'field': 'active_score_set_id', 'value': score_id}]
    assert len(app.state.runtime_manager.executor.jobs) == 1
    dispatch(app)
    accepted = client.post(f"/api/v1/experiments/{experiment['id']}/runs", json={'request_id': str(uuid4())})
    assert accepted.status_code == 202
    blocked_rescore = client.post('/api/v1/score-sets', json={'source_run_id': run_id})
    assert blocked_rescore.status_code == 409
    assert blocked_rescore.json()['fields'][0]['value'] == accepted.json()['run_id']


def test_rescore_storage_failure_after_attempt_stops_new_model_calls(experiment_api, monkeypatch):
    from agenteval import rescore_worker
    client, app, payload = experiment_api
    monkeypatch.setenv('RESCORE_TEST_KEY', 'private-rescore-canary')
    _, run_id = create_and_complete(client, app, payload)
    score_id = client.post('/api/v1/score-sets', json={'source_run_id': run_id,
        'judge_profile': {'base_url': 'http://judge.test', 'model': 'judge-double',
            'credential_ref': 'RESCORE_TEST_KEY', 'source_type': 'PROTOCOL_DOUBLE'}}).json()['id']
    app.state.runtime_manager.executor.jobs.clear()
    calls = []
    def fail_save(*args, **kwargs):
        raise SQLAlchemyError('private-attempt-storage-failure')
    monkeypatch.setattr(rescore_worker, 'append_judge_attempt', fail_save)
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'invalid-json'}}]})
    execute_rescore(app.state.session_factory, score_id, app.state.runtime_manager.owner_instance,
        ensure_owner=app.state.runtime_manager.ensure_owner,
        judge_factory=lambda snapshot, budget, guard: build_judge(snapshot, budget, guard, transport=httpx.MockTransport(handler)))
    assert len(calls) == 1
    assert client.get(f'/api/v1/score-sets/{score_id}').json()['status'] == 'FAILED'
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CaseEvaluation).where(CaseEvaluation.score_set_id == score_id)) == 0
        assert session.get(RuntimeControl, 1).active_job_id is None


def test_rescore_unexecuted_cases_stay_in_fixed_n_and_u(experiment_api):
    client, app, payload = experiment_api
    experiment = client.post('/api/v1/experiments', json=payload).json()
    run_id = client.post(f"/api/v1/experiments/{experiment['id']}/runs", json={'request_id': str(uuid4())}).json()['run_id']
    app.state.runtime_manager.executor.jobs.clear()
    factory = app.state.session_factory
    with factory.begin() as session:
        run = session.get(EvaluationRun, run_id)
        cases = list(session.scalars(select(CaseRun).where(CaseRun.run_id == run_id).order_by(CaseRun.case_key)))
        save_execution(session, cases[0].id, ExecutionResult(answer='已保存回答', agent_status='SUCCEEDED',
            source_type='MOCK', started_at=now(), ended_at=now(), duration_ms=1))
        cases[0].phase = 'INTERRUPTED'
        run.status = 'INTERRUPTED'
        session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id)).status = 'INTERRUPTED'
        slot = session.get(RuntimeControl, 1)
        slot.active_job_id = slot.active_job_type = slot.owner_instance = None
    score_id = client.post('/api/v1/score-sets', json={'source_run_id': run_id}).json()['id']
    app.state.runtime_manager.executor.jobs.clear()
    seen = []
    class PassedJudge:
        def evaluate(self, case, execution, **kwargs):
            seen.append(case['case_key'])
            return JudgeResult(status='SUCCEEDED', scores={metric: {'score': 5, 'reason': '合成测试评分'}
                for metric in ('correctness', 'relevance', 'task_completion')})
    execute_rescore(factory, score_id, app.state.runtime_manager.owner_instance,
                    ensure_owner=app.state.runtime_manager.ensure_owner, judge_factory=lambda *args: PassedJudge())
    summary = client.get(f'/api/v1/runs/{run_id}/scoring-summary?score_set_id={score_id}').json()['summary']
    assert (summary['N'], summary['P'], summary['F'], summary['U']) == (2, 1, 0, 1)
    assert seen == ['A'] and summary['agent']['status_counts']['NOT_EXECUTED'] == 1
    with factory() as session:
        cases = list(session.scalars(select(CaseRun).where(CaseRun.run_id == run_id).order_by(CaseRun.case_key)))
        assert [case.phase for case in cases] == ['INTERRUPTED', 'PENDING']
        assert cases[1].answer is None
        assert session.scalar(select(func.count()).select_from(CaseEvaluation).where(CaseEvaluation.score_set_id == score_id)) == 2


def test_request_id_cannot_cross_experiments_and_stale_start_rejected(experiment_api):
    from agenteval.runtime_schemas import StartRunRequest
    from agenteval.errors import ApiError
    client, app, payload = experiment_api
    first = client.post('/api/v1/experiments', json=payload).json()
    second = client.post('/api/v1/experiments', json=payload | {'name': '另一个实验'}).json()
    request_id = str(uuid4())
    initial = client.post(f"/api/v1/experiments/{first['id']}/runs", json={'request_id': request_id})
    assert initial.status_code == 202
    crossed = client.post(f"/api/v1/experiments/{second['id']}/runs", json={'request_id': request_id})
    assert crossed.status_code == 409 and crossed.json()['code'] == 'REQUEST_ID_PAYLOAD_MISMATCH'
    dispatch(app)
    # 模拟路由读取旧选择后、主控真正冻结前，另一个请求修改了实验版本。
    stale = StartRunRequest(request_id=str(uuid4()), dataset_id=payload['dataset_id'],
                           agent_config_id=payload['agent_config_id'], version_label=payload['version_label'])
    assert client.patch(f"/api/v1/experiments/{first['id']}", json={'version_label': '并发新标签'}).status_code == 200
    with pytest.raises(ApiError) as error:
        app.state.runtime_manager.accept(stale, experiment_id=first['id'])
    assert error.value.code == 'EXPERIMENT_CHANGED'
    assert app.state.runtime_manager.executor.jobs == []
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EvaluationRun)) == 1
        assert session.get(RuntimeControl, 1).active_job_id is None
