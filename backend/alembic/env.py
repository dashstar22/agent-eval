"""迁移仅使用显式配置的独立评测 schema。"""

import os

from alembic import context
from sqlalchemy import make_url

from agenteval.db import Base, make_engine
import agenteval.persistence  # noqa: F401

config = context.config
target_metadata = Base.metadata
database_url = os.environ.get("AGENTEVAL_DATABASE_URL")
if not database_url:
    raise RuntimeError("AGENTEVAL_DATABASE_URL_REQUIRED")
try:
    url = make_url(database_url)
except Exception:
    raise RuntimeError("AGENTEVAL_DATABASE_URL_INVALID") from None
# SQLite 仅供本机迁移测试，须显式声明；正式入口限定 MySQL/PyMySQL。
sqlite_test = url.drivername.startswith("sqlite") and os.environ.get("AGENTEVAL_ALLOW_SQLITE_TESTS") == "true"
if url.drivername != "mysql+pymysql" and not sqlite_test:
    raise RuntimeError("AGENTEVAL_DATABASE_DRIVER_REQUIRED")
# 只允许本项目命名空间，误填售后 schema 时在连接前拒绝。
if url.get_backend_name() == "mysql" and (not url.database or not url.database.startswith("agenteval_")):
    raise RuntimeError("AGENTEVAL_DATABASE_SCHEMA_PREFIX_REQUIRED")


def run_migrations_offline():
    context.configure(url=database_url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = make_engine(database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
