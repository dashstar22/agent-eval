"""人工复核追加历史与待审核回归候选；不修改自动评分。"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text, event, inspect
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, UTCDateTime, utcnow
from .persistence import FrozenJSON, MANAGEMENT_ID, freeze_json, new_id, same_run_constraints


class HumanReview(Base):
    __tablename__ = "human_reviews"
    __table_args__ = (
        *same_run_constraints("human_review"),
        CheckConstraint("conclusion IN ('PASS', 'FAIL', 'UNDETERMINED')", name="ck_human_review_conclusion"),
        CheckConstraint("human_confirmed = 1", name="ck_human_review_confirmed"),
        CheckConstraint("identity_type = 'SELF_DECLARED'", name="ck_human_review_identity"),
        Index("ix_human_review_latest", "run_id", "score_set_id", "case_run_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36))
    case_run_id: Mapped[str] = mapped_column(String(36))
    score_set_id: Mapped[str] = mapped_column(String(36))
    reviewer_name: Mapped[str] = mapped_column(String(200))
    identity_type: Mapped[str] = mapped_column(String(32), default="SELF_DECLARED")
    conclusion: Mapped[str] = mapped_column(String(32))
    scores: Mapped[dict[str, int]] = mapped_column(FrozenJSON(), default=dict)
    reason: Mapped[str] = mapped_column(Text)
    human_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RegressionDraft(Base):
    __tablename__ = "regression_drafts"
    __table_args__ = (
        *same_run_constraints("regression_draft"),
        CheckConstraint("status IN ('PENDING', 'APPROVED')", name="ck_regression_draft_status"),
        CheckConstraint("(status = 'PENDING' AND test_case_id IS NULL AND approved_by IS NULL AND approved_at IS NULL) OR "
                        "(status = 'APPROVED' AND test_case_id IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL)",
                        name="ck_regression_draft_approval"),
        Index("ix_regression_draft_target", "target_dataset_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36))
    case_run_id: Mapped[str] = mapped_column(String(36))
    score_set_id: Mapped[str] = mapped_column(String(36))
    target_dataset_id: Mapped[int] = mapped_column(MANAGEMENT_ID, ForeignKey("datasets.id", ondelete="RESTRICT"))
    case_key: Mapped[str] = mapped_column(String(100))
    case_snapshot: Mapped[dict[str, Any]] = mapped_column(FrozenJSON())
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    test_case_id: Mapped[int | None] = mapped_column(MANAGEMENT_ID, ForeignKey("test_cases.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


def _protect_review(*args):
    raise ValueError("HUMAN_REVIEW_APPEND_ONLY")


def _protect_draft_update(mapper, connection, target):
    state = inspect(target)
    immutable = ("run_id", "case_run_id", "score_set_id", "target_dataset_id", "case_key", "case_snapshot", "reason", "created_at")
    if any(state.attrs[name].history.has_changes() for name in immutable):
        raise ValueError("REGRESSION_DRAFT_SOURCE_IMMUTABLE")
    previous = state.attrs.status.history.deleted
    if previous and previous[0] == "APPROVED":
        raise ValueError("REGRESSION_DRAFT_ALREADY_APPROVED")
    if target.status == "APPROVED" and not previous:
        raise ValueError("REGRESSION_DRAFT_ALREADY_APPROVED")


event.listen(HumanReview, "before_update", _protect_review)
event.listen(HumanReview, "before_delete", _protect_review)
event.listen(HumanReview.scores, "set", lambda target, value, oldvalue, initiator: freeze_json(value), retval=True)
event.listen(RegressionDraft, "before_update", _protect_draft_update)
event.listen(RegressionDraft, "before_delete", _protect_review)
event.listen(RegressionDraft.case_snapshot, "set", lambda target, value, oldvalue, initiator: freeze_json(value), retval=True)
