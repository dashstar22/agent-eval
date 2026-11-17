"""人工复核、回归候选与有限重复批次；只增表及可空断言配置。

Revision ID: 20261117_0002
Revises: 20261116_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "20261117_0002"
down_revision = "20261116_0001"
branch_labels = None
depends_on = None


def _time():
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def _bigint():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _id():
    return sa.Column("id", sa.String(36), primary_key=True)


def _scope(prefix):
    return [
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("score_set_id", sa.String(36), nullable=False),
        sa.Column("case_run_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["score_set_id", "run_id"], ["score_sets.id", "score_sets.run_id"], name=f"fk_{prefix}_score_run", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["case_run_id", "run_id"], ["case_runs.id", "case_runs.run_id"], name=f"fk_{prefix}_case_run", ondelete="RESTRICT"),
    ]


def upgrade():
    # JSON 使用 NULL 表示旧行未配置；不重写任何历史 JSON 或评分。
    op.add_column("test_cases", sa.Column("tool_assertions", sa.JSON(), nullable=True))
    op.create_table("human_reviews", _id(), *_scope("human_review"),
        sa.Column("reviewer_name", sa.String(200), nullable=False),
        sa.Column("identity_type", sa.String(32), nullable=False),
        sa.Column("conclusion", sa.String(32), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("human_confirmed", sa.Boolean(), nullable=False), sa.Column("created_at", _time(), nullable=False),
        sa.CheckConstraint("conclusion IN ('PASS', 'FAIL', 'UNDETERMINED')", name="ck_human_review_conclusion"),
        sa.CheckConstraint("human_confirmed = 1", name="ck_human_review_confirmed"),
        sa.CheckConstraint("identity_type = 'SELF_DECLARED'", name="ck_human_review_identity"),
    )
    op.create_index("ix_human_review_latest", "human_reviews", ["run_id", "score_set_id", "case_run_id", "created_at"])
    op.create_table("regression_drafts", _id(), *_scope("regression_draft"),
        sa.Column("target_dataset_id", _bigint(), sa.ForeignKey("datasets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("case_key", sa.String(100), nullable=False), sa.Column("case_snapshot", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("approved_by", sa.String(200)), sa.Column("approved_at", _time()),
        sa.Column("test_case_id", _bigint(), sa.ForeignKey("test_cases.id", ondelete="RESTRICT")),
        sa.Column("created_at", _time(), nullable=False),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED')", name="ck_regression_draft_status"),
        sa.CheckConstraint("(status = 'PENDING' AND test_case_id IS NULL AND approved_by IS NULL AND approved_at IS NULL) OR (status = 'APPROVED' AND test_case_id IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL)", name="ck_regression_draft_approval"),
    )
    op.create_index("ix_regression_draft_target", "regression_drafts", ["target_dataset_id", "created_at"])
    op.create_table("repeat_batches", _id(),
        sa.Column("request_id", sa.String(36), nullable=False, unique=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("source_run_id", sa.String(36), sa.ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("repeat_count", sa.Integer(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_instance", sa.String(100), nullable=False), sa.Column("budget", sa.JSON(), nullable=False),
        sa.Column("created_at", _time(), nullable=False), sa.Column("started_at", _time()), sa.Column("ended_at", _time()),
        sa.Column("error", sa.JSON()),
    )
    op.create_table("repeat_members", _id(),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("repeat_batches.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("ordinal", sa.Integer(), nullable=False), sa.UniqueConstraint("batch_id", "ordinal", name="uq_repeat_ordinal"),
    )


def downgrade():
    for table in ("repeat_members", "repeat_batches", "regression_drafts", "human_reviews"):
        op.drop_table(table)
    op.drop_column("test_cases", "tool_assertions")
