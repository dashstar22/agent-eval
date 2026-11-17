"""真实事务、HTTP协议替身与锁/预算守卫；不属于真实模型质量证据。"""

import json
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from agenteval.db import Base, make_engine, session_factory
from agenteval.evaluation_store import load_summary_cases
from agenteval.models import ExecutionResult, now
from agenteval.persistence import (
    AgentConfig, CaseEvaluation, CaseRun, Dataset, EvaluationRun, JudgeAttempt,
    RuntimeControl, ScoreSet, TestCase,
)
from agenteval.runtime_adapters import build_adapter, build_judge
from agenteval.runtime_worker import _finish, execute_run
from agenteval.schemas import CaseCreate, FreezeRequest
from agenteval.snapshots import freeze_run
from agenteval.statistics import summarize

OWNER = "worker-test-owner"


@pytest.fixture
def database(tmp_path):
    engine = make_engine("sqlite+pysqlite:///" + (tmp_path / "worker.sqlite").as_posix())
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    yield factory
    engine.dispose()


def plan(factory, *, keys=("A", "B", "C"), adapter_type="HTTP", preconditions=None, **budget):
    with factory() as session, session.begin():
        dataset = Dataset(name="执行器事务测试")
        refs = {"username_ref": "WORKER_USER", "password_ref": "WORKER_PASSWORD"} if adapter_type == "PRINTER_HTTP" else {}
        agent = AgentConfig(name="协议替身", adapter_type=adapter_type,
                            base_url="http://agent.test", source_type="PROTOCOL_DOUBLE",
                            credential_refs=refs)
        session.add_all([dataset, agent])
        session.flush()
        for key in keys:
            session.add(TestCase(dataset_id=dataset.id, **CaseCreate(
                case_key=key, question="问题" + key, expected_answer="正确处理",
                preconditions=preconditions or {}).model_dump()))
        session.flush()
        request = FreezeRequest(dataset_id=dataset.id, agent_config_id=agent.id,
                                judge_profile={"base_url": "http://judge.test/v1", "model": "judge-double", "credential_ref": "WORKER_JUDGE_KEY"},
                                execution_budget=budget)
        run = freeze_run(session, request)
        run.status, run.owner_instance = "QUEUED", OWNER
        session.add(RuntimeControl(id=1, active_job_type="RUN", active_job_id=run.id, owner_instance=OWNER))
        return run.id


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("WORKER_USER", "test-user")
    monkeypatch.setenv("WORKER_PASSWORD", "private-worker-canary")
    monkeypatch.setenv("WORKER_JUDGE_KEY", "private-judge-canary")


def judge_response(content=None):
    content = content or json.dumps({"scores": {key: {"score": 5, "reason": "符合预期行为"}
                                                for key in ("correctness", "relevance", "task_completion")}})
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})


def adapter(handler):
    return lambda snapshot, budget, guard: build_adapter(snapshot, budget, guard, transport=httpx.MockTransport(handler))


def judge(handler=lambda _: judge_response()):
    return lambda snapshot, budget, guard: build_judge(snapshot, budget, guard, transport=httpx.MockTransport(handler))


def results(factory, run_id):
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        score = session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id))
        cases = list(session.scalars(select(CaseRun).where(CaseRun.run_id == run_id).order_by(CaseRun.case_key)))
        return run, score, cases


def test_middle_case_error_continues_and_quality_fail_does_not_fail_batch(database):
    run_id = plan(database)
    sent = []
    def agent(req):
        question = json.loads(req.content)["question"]
        sent.append(question)
        assert "expected_answer" not in json.loads(req.content)
        if question.endswith("B"):
            raise RuntimeError("authorization: Bearer private-worker-canary")
        return httpx.Response(200, json={"answer": "正确处理"})
    execute_run(database, run_id, OWNER, ensure_owner=lambda: None,
                adapter_factory=adapter(agent), judge_factory=judge())
    run, score, cases = results(database, run_id)
    assert run.status == score.status == "COMPLETED"
    assert sent == ["问题A", "问题B", "问题C"]
    assert [case.agent_status for case in cases] == ["SUCCEEDED", "FAILED", "SUCCEEDED"]
    with database() as session:
        summary = summarize(["A", "B", "C"], load_summary_cases(session, run_id, score.id))
        assert (summary["P"], summary["F"], summary["U"]) == (2, 1, 0)
        assert session.get(RuntimeControl, 1).active_job_id is None
        assert "private-worker-canary" not in json.dumps([case.raw_agent for case in cases])


def test_answer_and_each_attempt_committed_before_next_external_wait(database):
    run_id = plan(database, keys=("A",))
    observations = []
    def waiting_judge(req):
        # 独立连接可以在模型等待时读取回答；第一次错误也在重试前提交。
        with database() as session:
            case = session.scalar(select(CaseRun).where(CaseRun.run_id == run_id))
            attempts = session.scalar(select(func.count()).select_from(JudgeAttempt))
            assert case.answer == "已保存的回答" and case.phase == "SCORING"
            observations.append(attempts)
        return judge_response("invalid-json") if len(observations) == 1 else judge_response()
    execute_run(database, run_id, OWNER, ensure_owner=lambda: None,
                adapter_factory=adapter(lambda _: httpx.Response(200, json={"answer": "已保存的回答"})),
                judge_factory=judge(waiting_judge))
    assert observations == [0, 1]
    assert results(database, run_id)[0].status == "COMPLETED"
    with database() as session:
        attempts = list(session.scalars(select(JudgeAttempt).order_by(JudgeAttempt.attempt)))
        assert [item.status for item in attempts] == ["ERROR", "SUCCEEDED"]


def test_loss_between_judge_attempts_preserves_answer_and_stops_retry(database):
    run_id = plan(database, keys=("A", "B"))
    lost, calls = [False], []
    def ensure_owner():
        if lost[0]:
            raise RuntimeError("schema lock lost")
    def failing_judge(req):
        calls.append(req.url.path)
        lost[0] = True
        return judge_response("invalid-json")
    execute_run(database, run_id, OWNER, ensure_owner=ensure_owner,
                adapter_factory=adapter(lambda _: httpx.Response(200, json={"answer": "先保存"})),
                judge_factory=judge(failing_judge))
    run, score, cases = results(database, run_id)
    assert run.status == score.status == "INTERRUPTED" and cases[0].answer == "先保存"
    assert cases[1].answer is None and calls == ["/v1/chat/completions"]
    with database() as session:
        assert session.scalar(select(func.count()).select_from(JudgeAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(CaseEvaluation)) == 0
        assert session.get(RuntimeControl, 1).active_job_id is None


def test_total_call_budget_stops_judge_without_reexecuting_agent(database):
    run_id = plan(database, max_total_calls=1, total_timeout_seconds=30)
    calls = []
    def http(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"answer": "本题只允许一次Agent请求"})
    execute_run(database, run_id, OWNER, ensure_owner=lambda: None,
                adapter_factory=adapter(http), judge_factory=judge(lambda _: pytest.fail("Judge不得调用")))
    run, score, cases = results(database, run_id)
    assert run.status == score.status == "FAILED"
    assert run.error == {"code": "RUN_CALL_BUDGET_EXCEEDED"}
    assert len(calls) == 1 and cases[0].answer and cases[1].answer is None
    with database() as session:
        summary = summarize(["A", "B", "C"], load_summary_cases(session, run_id, score.id))
        assert (summary["P"], summary["F"], summary["U"]) == (0, 0, 3)


@pytest.mark.parametrize("mode", ["timeout", "oversize"])
def test_agent_timeout_and_response_limit_are_failed_facts(database, mode):
    run_id = plan(database, keys=("A",), response_max_bytes=256)
    def agent(req):
        if mode == "timeout":
            raise httpx.ReadTimeout("private-worker-canary", request=req)
        return httpx.Response(200, json={"answer": "x" * 500})
    execute_run(database, run_id, OWNER, ensure_owner=lambda: None,
                adapter_factory=adapter(agent), judge_factory=judge(lambda _: pytest.fail("无回答不得评判")))
    run, score, cases = results(database, run_id)
    assert run.status == "COMPLETED"
    assert cases[0].agent_status == ("TIMEOUT" if mode == "timeout" else "PROTOCOL_ERROR")
    assert cases[0].error["code"] == ("AGENT_TIMEOUT_RESULT_UNKNOWN" if mode == "timeout" else "RESPONSE_TOO_LARGE")


def test_persistence_failure_stops_without_new_external_calls(database, monkeypatch):
    run_id = plan(database)
    calls = []
    def fail_save(*_):
        raise RuntimeError("database write failed")
    monkeypatch.setattr("agenteval.runtime_worker.save_execution", fail_save)
    execute_run(database, run_id, OWNER, ensure_owner=lambda: None,
                adapter_factory=adapter(lambda req: calls.append(req.url) or httpx.Response(200, json={"answer": "回答"})),
                judge_factory=judge(lambda _: pytest.fail("保存失败不得调用Judge")))
    assert len(calls) == 1
    run, score, cases = results(database, run_id)
    assert run.status == score.status == "FAILED" and run.error == {"code": "RUNTIME_INFRASTRUCTURE_ERROR"}
    assert cases[1].answer is None


def test_duplicate_worker_does_not_complete_existing_running_job(database):
    run_id = plan(database)
    with database() as session, session.begin():
        session.get(EvaluationRun, run_id).status = "RUNNING"
    execute_run(database, run_id, OWNER, ensure_owner=lambda: None,
                adapter_factory=lambda *_: pytest.fail("重复工作线程不可调用Agent"))
    with database() as session:
        assert session.get(EvaluationRun, run_id).status == "RUNNING"
        assert session.get(RuntimeControl, 1).active_job_id == run_id


def test_lock_loss_after_printer_answer_prevents_observation_get(database):
    run_id = plan(database, keys=("A",), adapter_type="PRINTER_HTTP", preconditions={"order_id": 7})
    conversation, trace = str(uuid4()), str(uuid4())
    lost, calls = [False], []
    def ensure_owner():
        if lost[0]:
            raise RuntimeError("lost")
    def printer(req):
        path = req.url.path
        calls.append(path)
        if path == "/api/auth/csrf":
            return httpx.Response(200, json={"headerName": "X-CSRF-TOKEN", "token": "csrf"})
        if path == "/api/auth/login":
            return httpx.Response(200, json={})
        if path == "/api/orders/7":
            return httpx.Response(200, json={"id": 7})
        if path == "/api/conversations":
            return httpx.Response(201, json={"id": conversation})
        if path.endswith("/messages"):
            lost[0] = True
            return httpx.Response(201, json={"conversationId": conversation, "turnId": json.loads(req.content)["turnId"],
                                            "replayed": False, "assistantMessage": {"response": {"text": "已持久保存", "status": "ANSWERED"}}},
                                  headers={"X-Trace-Id": trace})
        pytest.fail("失去锁后出现观测或其他外部请求")
    execute_run(database, run_id, OWNER, ensure_owner=ensure_owner, adapter_factory=adapter(printer))
    run, score, cases = results(database, run_id)
    assert run.status == "INTERRUPTED" and cases[0].answer == "已持久保存"
    assert cases[0].observations["observation_status"] == "PENDING"
    assert not any(path.startswith("/api/evaluation-observations") for path in calls)


def test_multiturn_prerequisites_do_not_replace_target_execution_identity(database):
    run_id = plan(database, keys=("A",), adapter_type="PRINTER_HTTP",
                  preconditions={"order_id": 7, "prior_turns": ["先问一轮", "再补充"]}, max_agent_calls=3)
    seen = []
    class ExplicitTurns:
        def run(self, question, save_answer, **kwargs):
            prior = []
            for turn in [*kwargs["preconditions"], question]:
                result = ExecutionResult(answer="回答" + turn, agent_status="SUCCEEDED", source_type="PROTOCOL_DOUBLE",
                                         started_at=now(), ended_at=now(), duration_ms=1, prerequisites=prior.copy())
                save_answer(result)
                seen.append(result.execution_id)
                prior.append(result.model_dump(mode="json"))
            return result
    execute_run(database, run_id, OWNER, ensure_owner=lambda: None,
                adapter_factory=lambda *_: ExplicitTurns(), judge_factory=judge())
    run, score, cases = results(database, run_id)
    assert run.status == "COMPLETED" and cases[0].answer == "回答问题A"
    stored = cases[0].raw_agent["normalized_execution"]
    assert stored["execution_id"] == seen[-1] and len(stored["prerequisites"]) == 2


def test_expired_case_does_not_call_judge_and_preserves_successful_answer(database):
    run_id = plan(database, keys=("A",), case_timeout_seconds=1)
    def agent(req):
        time.sleep(1.01)
        return httpx.Response(200, json={"answer": "迟到响应"})
    execute_run(database, run_id, OWNER, ensure_owner=lambda: None,
                adapter_factory=adapter(agent), judge_factory=judge(lambda _: pytest.fail("过期不可调用Judge")))
    assert results(database, run_id)[0].status == "COMPLETED"
    with database() as session:
        assert session.scalar(select(func.count()).select_from(JudgeAttempt)) == 0


def test_late_finish_cannot_overwrite_recovered_run_or_new_owner_slot(database):
    run_id = plan(database, keys=("A",))
    with database() as session, session.begin():
        session.get(EvaluationRun, run_id).status = "RUNNING"
        session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id)).status = "RUNNING"
    stale = database()
    # 模拟旧执行者已读到RUNNING和旧活动槽，然后新进程恢复/接受新任务。
    # expire_on_commit=False保留旧对象，使原先 get→Python判断→ORM写回
    # 的实现确定性复现覆盖问题，不依赖不稳定的线程调度时间。
    with stale.begin():
        old_run = stale.get(EvaluationRun, run_id)
        old_slot = stale.get(RuntimeControl, 1)
        old_score = stale.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id))
    new_run_id, new_owner = str(uuid4()), "new-execution-owner"
    with database() as session, session.begin():
        session.get(EvaluationRun, run_id).status = "INTERRUPTED"
        session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id)).status = "INTERRUPTED"
        slot = session.get(RuntimeControl, 1)
        slot.active_job_id, slot.owner_instance = new_run_id, new_owner
    assert old_run.status == old_score.status == "RUNNING" and old_slot.owner_instance == OWNER
    _finish(lambda: stale, run_id, OWNER, "COMPLETED")
    with database() as session:
        assert session.get(EvaluationRun, run_id).status == "INTERRUPTED"
        assert session.scalar(select(ScoreSet).where(ScoreSet.run_id == run_id)).status == "INTERRUPTED"
        slot = session.get(RuntimeControl, 1)
        assert (slot.active_job_id, slot.owner_instance) == (new_run_id, new_owner)
