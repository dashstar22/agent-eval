"""建立独立评测领域表、历史关联及单活动槽。

Revision ID: 20261116_0001
Revises: None
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "20261116_0001"
down_revision = None
branch_labels = None
depends_on = None


def _time():
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def _bigint():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _id():
    return sa.Column("id", sa.String(36), primary_key=True)


def _timestamps():
    return [sa.Column("created_at", _time(), nullable=False), sa.Column("updated_at", _time(), nullable=False), sa.Column("deleted_at", _time())]


def _same_run(prefix):
    return [
        sa.ForeignKeyConstraint(["score_set_id", "run_id"], ["score_sets.id", "score_sets.run_id"], name=f"fk_{prefix}_score_run", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["case_run_id", "run_id"], ["case_runs.id", "case_runs.run_id"], name=f"fk_{prefix}_case_run", ondelete="RESTRICT"),
    ]


def _scoring_ids():
    return [sa.Column("run_id", sa.String(36), nullable=False), sa.Column("score_set_id", sa.String(36), nullable=False), sa.Column("case_run_id", sa.String(36), nullable=False)]


def upgrade():
    op.create_table("datasets",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("description", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), *_timestamps(),
    )
    op.create_index("ix_datasets_name", "datasets", ["name"])
    op.create_table("agent_configs",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("adapter_type", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False), sa.Column("timeout_ms", sa.Integer(), nullable=False),
        sa.Column("credential_refs", sa.JSON(), nullable=False), sa.Column("expected_version", sa.JSON(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), *_timestamps(),
    )
    op.create_index("ix_agent_configs_name", "agent_configs", ["name"])
    op.create_table("test_cases",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("dataset_id", _bigint(), sa.ForeignKey("datasets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("case_key", sa.String(100), nullable=False), sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected_answer", sa.Text(), nullable=False), sa.Column("expected_tools", sa.JSON()),
        sa.Column("tool_match_mode", sa.String(16), nullable=False), sa.Column("required_metrics", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("preconditions", sa.JSON(), nullable=False), sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("license", sa.String(100), nullable=False), sa.Column("seen_regression", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), *_timestamps(),
        sa.UniqueConstraint("dataset_id", "case_key", name="uq_test_case_key"),
    )
    op.create_index("ix_test_case_dataset_enabled", "test_cases", ["dataset_id", "enabled"])
    op.create_table("experiments",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("dataset_id", _bigint(), sa.ForeignKey("datasets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("agent_config_id", _bigint(), sa.ForeignKey("agent_configs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_label", sa.String(200), nullable=False), sa.Column("purpose", sa.Text(), nullable=False), *_timestamps(),
    )
    op.create_index("ix_experiment_created", "experiments", ["created_at"])
    op.create_table("evaluation_runs", _id(),
        sa.Column("experiment_id", _bigint(), sa.ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False), sa.Column("request_sha256", sa.String(64)),
        sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("observed_versions", sa.JSON(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_instance", sa.String(100)), sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("created_at", _time(), nullable=False), sa.Column("started_at", _time()), sa.Column("ended_at", _time()),
        sa.Column("error", sa.JSON()), sa.UniqueConstraint("request_id"),
    )
    op.create_index("ix_run_status_created", "evaluation_runs", ["status", "created_at"])
    op.create_table("case_runs", _id(),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("case_key", sa.String(100), nullable=False), sa.Column("case_snapshot", sa.JSON(), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False), sa.Column("agent_status", sa.String(32)), sa.Column("answer", sa.Text()),
        sa.Column("raw_agent", sa.JSON()), sa.Column("trace_id", sa.String(100)), sa.Column("observations", sa.JSON()),
        sa.Column("usage", sa.JSON()), sa.Column("timing", sa.JSON()), sa.Column("error", sa.JSON()),
        sa.UniqueConstraint("run_id", "case_key", name="uq_case_run_key"), sa.UniqueConstraint("id", "run_id", name="uq_case_run_id_run"),
    )
    op.create_index("ix_case_run_phase", "case_runs", ["run_id", "phase"])
    op.create_table("score_sets", _id(),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("parent_score_set_id", sa.String(36)), sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("original_run_key", sa.String(36), sa.Computed("CASE WHEN kind = 'ORIGINAL' THEN run_id ELSE NULL END", persisted=True)),
        sa.Column("judge_snapshot", sa.JSON(), nullable=False), sa.Column("rule_snapshot", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", _time(), nullable=False),
        sa.UniqueConstraint("id", "run_id", name="uq_score_set_id_run"), sa.UniqueConstraint("original_run_key", name="uq_score_set_original"),
        sa.ForeignKeyConstraint(["parent_score_set_id", "run_id"], ["score_sets.id", "score_sets.run_id"], name="fk_score_parent_run", ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('ORIGINAL', 'RESCORE')", name="ck_score_set_kind"),
        sa.CheckConstraint("(kind = 'ORIGINAL' AND parent_score_set_id IS NULL) OR (kind = 'RESCORE' AND parent_score_set_id IS NOT NULL)", name="ck_score_set_parent"),
    )
    op.create_table("case_evaluations", _id(), *_scoring_ids(),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("conclusion", sa.String(32)), sa.Column("completed_at", _time()),
        *_same_run("case_eval"), sa.UniqueConstraint("score_set_id", "case_run_id", name="uq_case_eval_score_case"),
    )
    op.create_index("ix_case_eval_conclusion", "case_evaluations", ["score_set_id", "conclusion"])
    op.create_table("judge_attempts", _id(), *_scoring_ids(),
        sa.Column("attempt", sa.Integer(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("request", sa.JSON()), sa.Column("response", sa.JSON()), sa.Column("usage", sa.JSON()),
        sa.Column("duration_ms", sa.Integer()), sa.Column("error", sa.JSON()),
        *_same_run("judge"), sa.UniqueConstraint("score_set_id", "case_run_id", "attempt", name="uq_judge_attempt"),
    )
    op.create_table("evaluation_scores", _id(), *_scoring_ids(),
        sa.Column("metric", sa.String(100), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("score", sa.Float()), sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text()), sa.Column("evidence", sa.JSON()),
        *_same_run("metric"), sa.UniqueConstraint("score_set_id", "case_run_id", "metric", name="uq_evaluation_metric"),
    )
    op.create_table("agent_traces", _id(),
        sa.Column("case_run_id", sa.String(36), sa.ForeignKey("case_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_id", sa.String(100), nullable=False), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False), sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200)), sa.Column("status", sa.String(100), nullable=False),
        sa.Column("duration_ms", sa.Integer()), sa.Column("payload", sa.JSON()), sa.Column("completeness", sa.String(16), nullable=False),
        sa.UniqueConstraint("case_run_id", "event_id", name="uq_agent_trace_event"),
    )
    op.create_index("ix_agent_trace_sequence", "agent_traces", ["case_run_id", "sequence"])
    op.create_table("badcase_annotations", _id(), *_scoring_ids(),
        sa.Column("manual_tags", sa.JSON(), nullable=False), sa.Column("note", sa.Text(), nullable=False), sa.Column("updated_at", _time(), nullable=False),
        *_same_run("annotation"), sa.UniqueConstraint("case_run_id", "score_set_id", name="uq_badcase_annotation"),
    )
    control = op.create_table("runtime_control",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False), sa.Column("active_job_type", sa.String(32)),
        sa.Column("active_job_id", sa.String(36)), sa.Column("owner_instance", sa.String(100)),
        sa.CheckConstraint("id = 1", name="ck_runtime_singleton"),
    )
    op.bulk_insert(control, [{"id": 1, "active_job_type": None, "active_job_id": None, "owner_instance": None}])


def downgrade():
    # 初始领域库含不可覆盖历史。回退须另行审核备份/兼容方案，不自动删表。
    raise RuntimeError("INITIAL_MIGRATION_REQUIRES_BACKUP_RESTORE_PLAN")
