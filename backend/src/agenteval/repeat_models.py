"""重复执行的批次及成员；执行事实仍在原有 run / score_set 表。"""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, UTCDateTime, utcnow
from .persistence import new_id


class RepeatBatch(Base):
    __tablename__ = "repeat_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    source_run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="RESTRICT"))
    repeat_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED")
    owner_instance: Mapped[str] = mapped_column(String(100))
    budget: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class RepeatMember(Base):
    __tablename__ = "repeat_members"
    __table_args__ = (UniqueConstraint("batch_id", "ordinal", name="uq_repeat_ordinal"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("repeat_batches.id", ondelete="RESTRICT"))
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), unique=True)
    ordinal: Mapped[int] = mapped_column(Integer)
