"""独立评测库连接；启动应用不隐式建表或迁移。"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """MySQL DATETIME(6) 按 UTC 存储，读取恢复明确时区。"""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DATETIME(fsp=6) if dialect.name == "mysql" else DateTime())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTC_TIMESTAMP_REQUIRES_TIMEZONE")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


def make_engine(url: str, **kwargs):
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if str(url).startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    options.update(kwargs)
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    elif engine.dialect.name == "mysql":
        @event.listens_for(engine, "connect")
        def mysql_utc(connection, _):
            with connection.cursor() as cursor:
                cursor.execute("SET time_zone = '+00:00'")
    return engine


def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
