"""将可追溯合成题一次性写入独立评测库；重复执行不覆盖用户编辑。"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.engine import make_url

from .db import make_engine, session_factory
from .persistence import Dataset, TestCase
from .schemas import CaseCreate

DEFAULT_DATA = Path(__file__).resolve().parents[3] / "data" / "printer-demo" / "dataset.json"


def load_demo(path: str | Path | None = None) -> dict:
    """校验本地副本和全部用例；不读取或修改外部售后工作区。"""
    source_path = Path(path) if path is not None else DEFAULT_DATA
    asset = json.loads(source_path.read_text(encoding="utf-8"))
    manifest = json.loads((source_path.parent / asset["source_manifest"]).read_text(encoding="utf-8"))
    for name, expected_hash in manifest["copied_sources"].items():
        if Path(name).name != name:
            raise ValueError("DEMO_SOURCE_PATH_INVALID")
        actual = hashlib.sha256((source_path.parent / "sources" / name).read_bytes()).hexdigest()
        if actual != expected_hash:
            raise ValueError("DEMO_SOURCE_HASH_MISMATCH")
    cases = [CaseCreate.model_validate(case).model_dump(mode="json") for case in asset["cases"]]
    if len(cases) < 30 or any(not case["enabled"] for case in cases):
        raise ValueError("DEMO_REQUIRES_30_ENABLED_CASES")
    if len({case["case_key"] for case in cases}) != len(cases):
        raise ValueError("DEMO_DUPLICATE_CASE_KEY")
    if any(case["license"] != "CC0-1.0" or not case["seen_regression"] for case in cases):
        raise ValueError("DEMO_PROVENANCE_INVALID")
    return {**asset, "cases": cases}


def seed_demo(session, path: str | Path | None = None) -> dict:
    """使用调用方事务，发生任何冲突均不改已有内容；本函数不提交事务。"""
    asset = load_demo(path)
    existing = list(session.scalars(select(Dataset).where(or_(
        Dataset.name == asset["name"],
        Dataset.description.contains(asset["dataset_key"]),
    ))))
    if len(existing) > 1:
        raise ValueError("DEMO_DATASET_IDENTITY_CONFLICT")
    created = not existing
    if existing:
        dataset = existing[0]
        current = list(session.scalars(select(TestCase).where(TestCase.dataset_id == dataset.id)))
        expected = {case["case_key"]: case for case in asset["cases"]}
        if (dataset.deleted_at is not None or dataset.name != asset["name"]
                or dataset.description != asset["description"] or len(current) != len(expected)):
            raise ValueError("DEMO_ALREADY_EXISTS_WITH_CHANGES")
        for case in current:
            target = expected.get(case.case_key)
            if case.deleted_at is not None or target is None or any(
                getattr(case, key) != value for key, value in target.items()
            ):
                raise ValueError("DEMO_ALREADY_EXISTS_WITH_CHANGES")
    else:
        dataset = Dataset(name=asset["name"], description=asset["description"])
        session.add(dataset)
        session.flush()
        session.add_all(TestCase(dataset_id=dataset.id, **case) for case in asset["cases"])
        session.flush()
    return {
        "status": "CREATED" if created else "ALREADY_PRESENT",
        "dataset_id": dataset.id,
        "dataset_key": asset["dataset_key"],
        "enabled_cases": len(asset["cases"]),
        "source_type": "SYNTHETIC_SEEN_REGRESSION",
        "external_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="向已迁移的独立MySQL评测库初始化30道合成回归题")
    parser.add_argument("--data", type=Path, help="替代dataset.json路径；默认使用仓库内数据")
    args = parser.parse_args()
    engine = None
    try:
        url = os.environ.get("AGENTEVAL_DATABASE_URL", "")
        parsed = make_url(url)
        if parsed.get_backend_name() != "mysql" or not (parsed.database or "").startswith("agenteval_"):
            raise ValueError("DEDICATED_AGENTEVAL_MYSQL_SCHEMA_REQUIRED")
        engine = make_engine(url)
        with session_factory(engine).begin() as session:
            result = seed_demo(session, args.data)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        safe_codes = {
            "DEDICATED_AGENTEVAL_MYSQL_SCHEMA_REQUIRED", "DEMO_SOURCE_PATH_INVALID",
            "DEMO_SOURCE_HASH_MISMATCH", "DEMO_REQUIRES_30_ENABLED_CASES", "DEMO_DUPLICATE_CASE_KEY",
            "DEMO_PROVENANCE_INVALID", "DEMO_DATASET_IDENTITY_CONFLICT", "DEMO_ALREADY_EXISTS_WITH_CHANGES",
        }
        code = str(exc) if isinstance(exc, ValueError) and str(exc) in safe_codes else "DEMO_INITIALIZATION_FAILED"
        parser.exit(1, code + "\n")
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
