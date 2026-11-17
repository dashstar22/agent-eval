"""生产入口只接独立MySQL评测库；SQLite仅可显式注入测试引擎。"""

import os

from sqlalchemy.engine import make_url


def database_url():
    value = os.environ.get("AGENTEVAL_DATABASE_URL", "")
    try:
        url = make_url(value)
        if url.drivername != "mysql+pymysql" or not url.database or not url.database.startswith("agenteval_"):
            raise ValueError()
    except Exception:
        raise ValueError("AGENTEVAL_DATABASE_URL_REQUIRED_MYSQL_AGENTEVAL_SCHEMA") from None
    return value
