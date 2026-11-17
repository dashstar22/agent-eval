"""管理源数据与不可变运行输入分开保存；历史关联由数据库再次校验。"""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Computed, Float, ForeignKey,
    ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint,
    event, inspect,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from .db import Base, UTCDateTime, utcnow

MANAGEMENT_ID = BigInteger().with_variant(Integer, "sqlite")


def new_id() -> str:
    return str(uuid4())


def _immutable(*args, **kwargs):
    raise TypeError("IMMUTABLE_SNAPSHOT")


class FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


class FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, memo):
        return self


def freeze_json(value):
    if isinstance(value, dict):
        return FrozenDict({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(freeze_json(item) for item in value)
    return value


class FrozenJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def process_result_value(self, value, dialect):
        return freeze_json(value)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class SoftDelete:
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class Dataset(Timestamps, SoftDelete, Base):
    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(MANAGEMENT_ID, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class TestCase(Timestamps, SoftDelete, Base):
    __tablename__ = "test_cases"
    __test__ = False
    __table_args__ = (
        UniqueConstraint("dataset_id", "case_key", name="uq_test_case_key"),
        Index("ix_test_case_dataset_enabled", "dataset_id", "enabled"),
    )
    id: Mapped[int] = mapped_column(MANAGEMENT_ID, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="RESTRICT"))
    case_key: Mapped[str] = mapped_column(String(100))
    question: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str] = mapped_column(Text)
    expected_tools: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    tool_match_mode: Mapped[str] = mapped_column(String(16), default="contains")
    tool_assertions: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict, nullable=True)
    required_metrics: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    preconditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    license: Mapped[str] = mapped_column(String(100), default="UNSPECIFIED")
    seen_regression: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class AgentConfig(Timestamps, SoftDelete, Base):
    __tablename__ = "agent_configs"
    id: Mapped[int] = mapped_column(MANAGEMENT_ID, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    adapter_type: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(2048))
    timeout_ms: Mapped[int] = mapped_column(Integer, default=195000)
    credential_refs: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    expected_version: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_type: Mapped[str] = mapped_column(String(32), default="REAL_EXTERNAL")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Experiment(Timestamps, SoftDelete, Base):
    __tablename__ = "experiments"
    __table_args__ = (Index("ix_experiment_created", "created_at"),)
    id: Mapped[int] = mapped_column(MANAGEMENT_ID, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="RESTRICT"))
    agent_config_id: Mapped[int] = mapped_column(ForeignKey("agent_configs.id", ondelete="RESTRICT"))
    version_label: Mapped[str] = mapped_column(String(200), default="")
    purpose: Mapped[str] = mapped_column(Text, default="")


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (Index("ix_run_status_created", "status", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="RESTRICT"))
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(FrozenJSON())
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    observed_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="FROZEN")
    owner_instance: Mapped[str | None] = mapped_column(String(100))
    total_cases: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class CaseRun(Base):
    __tablename__ = "case_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "case_key", name="uq_case_run_key"),
        UniqueConstraint("id", "run_id", name="uq_case_run_id_run"),
        Index("ix_case_run_phase", "run_id", "phase"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="RESTRICT"))
    case_key: Mapped[str] = mapped_column(String(100))
    case_snapshot: Mapped[dict[str, Any]] = mapped_column(FrozenJSON())
    phase: Mapped[str] = mapped_column(String(32), default="FROZEN")
    agent_status: Mapped[str | None] = mapped_column(String(32))
    answer: Mapped[str | None] = mapped_column(Text)
    raw_agent: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(100))
    observations: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    timing: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class ScoreSet(Base):
    __tablename__ = "score_sets"
    __table_args__ = (
        UniqueConstraint("id", "run_id", name="uq_score_set_id_run"),
        UniqueConstraint("original_run_key", name="uq_score_set_original"),
        ForeignKeyConstraint(["parent_score_set_id", "run_id"], ["score_sets.id", "score_sets.run_id"], name="fk_score_parent_run", ondelete="RESTRICT"),
        CheckConstraint("kind IN ('ORIGINAL', 'RESCORE')", name="ck_score_set_kind"),
        CheckConstraint("(kind = 'ORIGINAL' AND parent_score_set_id IS NULL) OR (kind = 'RESCORE' AND parent_score_set_id IS NOT NULL)", name="ck_score_set_parent"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="RESTRICT"))
    parent_score_set_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(16), default="ORIGINAL")
    original_run_key: Mapped[str | None] = mapped_column(String(36), Computed("CASE WHEN kind = 'ORIGINAL' THEN run_id ELSE NULL END", persisted=True))
    judge_snapshot: Mapped[dict[str, Any]] = mapped_column(FrozenJSON())
    rule_snapshot: Mapped[dict[str, Any]] = mapped_column(FrozenJSON())
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="FROZEN")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


def same_run_constraints(prefix):
    return (
        ForeignKeyConstraint(["score_set_id", "run_id"], ["score_sets.id", "score_sets.run_id"], name=f"fk_{prefix}_score_run", ondelete="RESTRICT"),
        ForeignKeyConstraint(["case_run_id", "run_id"], ["case_runs.id", "case_runs.run_id"], name=f"fk_{prefix}_case_run", ondelete="RESTRICT"),
    )


class CaseEvaluation(Base):
    __tablename__ = "case_evaluations"
    __table_args__ = (*same_run_constraints("case_eval"), UniqueConstraint("score_set_id", "case_run_id", name="uq_case_eval_score_case"), Index("ix_case_eval_conclusion", "score_set_id", "conclusion"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36))
    score_set_id: Mapped[str] = mapped_column(String(36))
    case_run_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    conclusion: Mapped[str | None] = mapped_column(String(32))
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class JudgeAttempt(Base):
    __tablename__ = "judge_attempts"
    __table_args__ = (*same_run_constraints("judge"), UniqueConstraint("score_set_id", "case_run_id", "attempt", name="uq_judge_attempt"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36))
    score_set_id: Mapped[str] = mapped_column(String(36))
    case_run_id: Mapped[str] = mapped_column(String(36))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    request: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class EvaluationScore(Base):
    __tablename__ = "evaluation_scores"
    __table_args__ = (*same_run_constraints("metric"), UniqueConstraint("score_set_id", "case_run_id", "metric", name="uq_evaluation_metric"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36))
    score_set_id: Mapped[str] = mapped_column(String(36))
    case_run_id: Mapped[str] = mapped_column(String(36))
    metric: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16))
    score: Mapped[float | None] = mapped_column(Float)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class AgentTrace(Base):
    __tablename__ = "agent_traces"
    __table_args__ = (UniqueConstraint("case_run_id", "event_id", name="uq_agent_trace_event"), Index("ix_agent_trace_sequence", "case_run_id", "sequence"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_run_id: Mapped[str] = mapped_column(ForeignKey("case_runs.id", ondelete="RESTRICT"))
    event_id: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(100))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    completeness: Mapped[str] = mapped_column(String(16), default="UNAVAILABLE")


class BadcaseAnnotation(Base):
    __tablename__ = "badcase_annotations"
    __table_args__ = (*same_run_constraints("annotation"), UniqueConstraint("case_run_id", "score_set_id", name="uq_badcase_annotation"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36))
    case_run_id: Mapped[str] = mapped_column(String(36))
    score_set_id: Mapped[str] = mapped_column(String(36))
    manual_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class RuntimeControl(Base):
    __tablename__ = "runtime_control"
    __table_args__ = (CheckConstraint("id = 1", name="ck_runtime_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    active_job_type: Mapped[str | None] = mapped_column(String(32))
    active_job_id: Mapped[str | None] = mapped_column(String(36))
    owner_instance: Mapped[str | None] = mapped_column(String(100))


_IMMUTABLE = {
    EvaluationRun: ("experiment_id", "request_id", "snapshot", "snapshot_sha256", "total_cases", "request_sha256"),
    CaseRun: ("run_id", "case_key", "case_snapshot"),
    ScoreSet: ("run_id", "parent_score_set_id", "kind", "judge_snapshot", "rule_snapshot", "fingerprint"),
}


def _protect_snapshot(mapper, connection, target):
    state = inspect(target)
    if any(state.attrs[name].history.has_changes() for name in _IMMUTABLE[type(target)]):
        raise ValueError("IMMUTABLE_SNAPSHOT")


def _protect_history_delete(mapper, connection, target):
    raise ValueError("HISTORY_DELETE_FORBIDDEN")


for _model in _IMMUTABLE:
    event.listen(_model, "before_update", _protect_snapshot)
    event.listen(_model, "before_delete", _protect_history_delete)

for _model, _attributes in ((EvaluationRun, ("snapshot",)), (CaseRun, ("case_snapshot",)), (ScoreSet, ("judge_snapshot", "rule_snapshot"))):
    for _attribute in _attributes:
        event.listen(getattr(_model, _attribute), "set", lambda target, value, oldvalue, initiator: freeze_json(value), retval=True)

# 统一注册新增领域表，Base.metadata 与迁移测试看到同一份模型。
from . import review_models, repeat_models  # noqa: E402,F401
