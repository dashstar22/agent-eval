"""只冻结计划，不调用Agent/Judge、不排队。"""

import hashlib
import json
from importlib.metadata import version
from uuid import uuid4

from sqlalchemy import select

from .errors import ApiError
from .persistence import AgentConfig, CaseRun, Dataset, EvaluationRun, Experiment, ScoreSet, TestCase
from .privacy import known_secrets, sanitize
from .services import active, agent_content, case_content, resolve_credentials


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def freeze_run(session, request, *, request_id=None, request_sha256=None,
               status="FROZEN", owner_instance=None, experiment_id=None):
    dataset = active(session, Dataset, request.dataset_id)
    config = active(session, AgentConfig, request.agent_config_id)
    if not config.enabled:
        raise ApiError(422, "AGENT_CONFIG_DISABLED", "停用配置不能用于新运行")
    cases = session.scalars(select(TestCase).where(TestCase.dataset_id == dataset.id, TestCase.enabled.is_(True), TestCase.deleted_at.is_(None)).order_by(TestCase.case_key).with_for_update()).all()
    if not cases:
        raise ApiError(422, "NO_ENABLED_CASES", "数据集没有启用用例")
    # 冻结不发请求，但引用必须可解析，避免生成不能执行的连接计划。
    resolve_credentials(config)
    secrets = known_secrets((config.credential_refs or {}) | {"judge_ref": request.judge_profile.credential_ref})
    connection = agent_content(config)
    refs = connection.pop("credential_refs")
    connection = sanitize(connection, secrets)
    connection["credential_refs"] = refs
    judge = request.judge_profile.model_dump()
    judge_ref = judge.pop("credential_ref")
    judge = sanitize(judge, secrets)
    judge["credential_ref"] = judge_ref
    frozen_cases = [sanitize(case_content(case) | {"revision": case.revision}, secrets) for case in cases]
    snapshot = {
        "schema_version": "agenteval-snapshot-v1",
        "dataset": sanitize({"id": dataset.id, "name": dataset.name, "description": dataset.description, "revision": dataset.revision}, secrets),
        "cases": frozen_cases,
        "agent": connection | {"id": config.id, "revision": config.revision},
        "candidate_version_label": sanitize(request.version_label, secrets),
        "judge": judge, "rules": sanitize(request.rule_profile.model_dump(), secrets),
        "execution_budget": request.execution_budget.model_dump(),
        "components": {"agenteval": version("agenteval"), "snapshot": "v1"},
    }
    if experiment_id is None:
        experiment = Experiment(name=f"冻结输入 {dataset.name}"[:200], dataset_id=dataset.id, agent_config_id=config.id, version_label=snapshot["candidate_version_label"], purpose="GROUP2_FREEZE_ONLY" if status == "FROZEN" else "RUNTIME_EXECUTION")
        session.add(experiment)
        session.flush()
    else:
        # 与实验编辑事务互斥，读取路由选择之后的当前值并持锁至冻结提交。
        experiment = session.scalar(select(Experiment).where(Experiment.id == experiment_id).with_for_update())
        if experiment is None or experiment.deleted_at is not None:
            raise ApiError(404, "RESOURCE_NOT_FOUND", "实验不存在或已删除")
        if experiment.dataset_id != dataset.id or experiment.agent_config_id != config.id or experiment.version_label != request.version_label:
            raise ApiError(409, "EXPERIMENT_CHANGED", "实验配置已变更，请刷新后重新启动")
    snapshot["experiment"] = sanitize({"id": experiment.id, "name": experiment.name,
                                       "version_label": experiment.version_label, "purpose": experiment.purpose}, secrets)
    # 以JSON往返分离所有可变对象。
    snapshot = json.loads(canonical_json(snapshot))
    run = EvaluationRun(experiment_id=experiment.id, request_id=request_id or str(uuid4()),
                        request_sha256=request_sha256, snapshot=snapshot,
                        snapshot_sha256=digest(snapshot), status=status, owner_instance=owner_instance,
                        total_cases=len(cases), observed_versions={"verification": "UNKNOWN"})
    session.add(run)
    session.flush()
    original = ScoreSet(run_id=run.id, kind="ORIGINAL", judge_snapshot=judge, rule_snapshot=snapshot["rules"],
                        fingerprint=digest({"judge": judge, "rules": snapshot["rules"]}), status="PENDING")
    session.add(original)
    for content in frozen_cases:
        session.add(CaseRun(run_id=run.id, case_key=content["case_key"], case_snapshot=content, phase="PENDING"))
    session.flush()
    return run
