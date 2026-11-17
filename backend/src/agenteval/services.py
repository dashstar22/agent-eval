"""管理事务与历史读取，路由只负责HTTP映射。"""

from datetime import datetime, timezone
from typing import Mapping

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .errors import ApiError
from .persistence import AgentConfig, CaseRun, Dataset, EvaluationRun, ScoreSet, TestCase
from .privacy import bounded_raw, known_secrets, sanitize
from .schemas import AgentCreate, CaseCreate


def utcnow():
    return datetime.now(timezone.utc)


def active(session: Session, cls, identity):
    row = session.get(cls, identity)
    if row is None or row.deleted_at is not None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "资源不存在或已删除")
    return row


def locate_case(session: Session, dataset_id: int, case_key: str):
    active(session, Dataset, dataset_id)
    row = session.scalar(select(TestCase).where(TestCase.dataset_id == dataset_id, TestCase.case_key == case_key, TestCase.deleted_at.is_(None)))
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "用例不存在或已删除")
    return row


def page_of(session: Session, stmt, page: int, page_size: int, serializer):
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = session.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [serializer(r) for r in rows], "total": total, "page": page, "page_size": page_size}


def public_row(row, names):
    return {name: getattr(row, name) for name in names.split()}


def dataset_view(session: Session, row):
    result = public_row(row, "id name description revision created_at updated_at")
    result["enabled_cases"] = session.scalar(select(func.count()).select_from(TestCase).where(TestCase.dataset_id == row.id, TestCase.enabled.is_(True), TestCase.deleted_at.is_(None)))
    result["links"] = {"self": f"/api/v1/datasets/{row.id}", "cases": f"/api/v1/datasets/{row.id}/cases"}
    return sanitize(result, known_secrets())


CASE_FIELDS = "case_key question expected_answer expected_tools tool_match_mode required_metrics tags enabled preconditions source license seen_regression"


def case_content(row):
    return public_row(row, CASE_FIELDS)


def case_view(row):
    result = public_row(row, "id dataset_id revision created_at updated_at") | case_content(row)
    result["links"] = {"self": f"/api/v1/datasets/{row.dataset_id}/cases/{row.case_key}"}
    return sanitize(result, known_secrets())


AGENT_FIELDS = "name adapter_type base_url timeout_ms credential_refs expected_version enabled source_type"


def agent_content(row):
    # typed引用为变量名称，绝不解析后序列化。
    return public_row(row, AGENT_FIELDS)


def resolve_credentials(row, environment: Mapping[str, str] | None = None):
    import os
    env = os.environ if environment is None else environment
    values = {}
    for key, name in (row.credential_refs or {}).items():
        if name:
            if not env.get(name):
                raise ApiError(422, "CREDENTIAL_REFERENCE_UNRESOLVED", "凭证环境引用未解析", [{"field": f"credential_refs.{key}", "code": "unresolved"}])
            values[key.removesuffix("_ref")] = env[name]
    return values


def agent_view(row):
    refs = row.credential_refs or {}
    result = agent_content(row) | public_row(row, "id revision created_at updated_at")
    # 引用名经过schema校验，可公开；不把password_ref误当密码值涂去。
    result = sanitize({k: v for k, v in result.items() if k != "credential_refs"}, known_secrets(refs))
    result["credential_refs"] = refs
    import os
    result["credential_status"] = {key: "RESOLVED" if os.environ.get(name) else "UNRESOLVED" for key, name in refs.items() if name}
    result["links"] = {"self": f"/api/v1/agent-configs/{row.id}"}
    return result


def write_case(session, dataset, data):
    if session.scalar(select(TestCase.id).where(TestCase.dataset_id == dataset.id, TestCase.case_key == data.case_key)) is not None:
        raise ApiError(409, "DUPLICATE_CASE_KEY", "同一数据集的 case_key 已存在（含已删除历史）", [{"field": "case_key", "code": "duplicate"}])
    values = sanitize(data.model_dump(), known_secrets())
    # 再校验脱敏后的结构，禁止安全过滤产生非法字段。
    values = CaseCreate.model_validate(values).model_dump()
    row = TestCase(dataset_id=dataset.id, **values)
    session.add(row)
    dataset.revision += 1
    session.flush()
    return row


def patch_case(session, row, patch):
    data = CaseCreate.model_validate(case_content(row) | patch.model_dump(exclude_unset=True))
    for key, value in sanitize(data.model_dump(), known_secrets()).items():
        setattr(row, key, value)
    row.revision += 1
    dataset = session.get(Dataset, row.dataset_id)
    dataset.revision += 1
    session.flush()
    return row


def patch_agent(session, row, patch):
    data = AgentCreate.model_validate(agent_content(row) | patch.model_dump(exclude_unset=True))
    values = data.model_dump()
    refs = values.pop("credential_refs")
    values = sanitize(values, known_secrets(refs)) | {"credential_refs": refs}
    for key, value in values.items():
        setattr(row, key, value)
    row.revision += 1
    session.flush()
    return row


def soft_delete(session, row):
    row.deleted_at = utcnow()
    if hasattr(row, "revision"):
        row.revision += 1
    if isinstance(row, TestCase):
        session.get(Dataset, row.dataset_id).revision += 1
    session.flush()


def run_view(row):
    result = public_row(row, "id experiment_id status total_cases snapshot snapshot_sha256 observed_versions created_at")
    result["execution_available"] = False
    result["links"] = {"self": f"/api/v1/runs/{row.id}", "cases": f"/api/v1/runs/{row.id}/cases", "report_input": f"/api/v1/runs/{row.id}/report-input"}
    return result


def case_run_view(row):
    result = public_row(row, "id run_id case_key case_snapshot phase agent_status answer raw_agent trace_id observations usage timing error")
    result["links"] = {"self": f"/api/v1/case-runs/{row.id}"}
    return sanitize(result, known_secrets())


def report_input(session, run):
    # 第2组提供历史报告的输入；评分/聚合算法在后续组实现。
    sets = session.scalars(select(ScoreSet).where(ScoreSet.run_id == run.id).order_by(ScoreSet.created_at, ScoreSet.id)).all()
    return {"run": run_view(run), "score_sets": [public_row(s, "id run_id parent_score_set_id kind judge_snapshot rule_snapshot fingerprint status") for s in sets], "report_status": "NOT_IMPLEMENTED"}


def save_raw_result(session, case_run, raw):
    case_run.raw_agent = bounded_raw(raw, known_secrets())
    session.flush()
