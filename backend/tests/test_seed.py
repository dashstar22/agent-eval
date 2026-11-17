"""合成资料与已见八问的完整性、幂等导入和用户编辑保护。"""

import hashlib
import json
from pathlib import Path
import shutil

import pytest
from sqlalchemy import func, select

from agenteval.db import Base, make_engine, session_factory
from agenteval.persistence import Dataset, TestCase as CaseRow
from agenteval.seed import DEFAULT_DATA, load_demo, seed_demo


def test_demo_cases_preserve_source_and_cover_required_scenarios():
    asset = load_demo()
    cases = {case["case_key"]: case for case in asset["cases"]}
    assert len(cases) == 30
    assert all(case["enabled"] and case["seen_regression"] for case in cases.values())
    assert all(case["expected_answer"] and case["source"] and case["license"] == "CC0-1.0"
               and case["preconditions"]["order_id"] is None for case in cases.values())
    original = json.loads((DEFAULT_DATA.parent / "sources" / "questions.json").read_text(encoding="utf-8"))
    for source in original["cases"]:
        current = cases[source["id"]]
        assert current["question"] == source["question"]
        assert current["source"]["original_required"] == source.get("required")
        assert current["source"]["original_forbidden"] == source.get("forbidden", [])
        assert current["source"]["seen_attributes"]["status"] == "SEEN_REGRESSION_NOT_UNSEEN_HOLDOUT"
    tags = {tag for case in cases.values() for tag in case["tags"]}
    assert {"连接无依据", "卡纸澄清", "模糊浅淡", "耗材", "故障码", "无依据", "澄清"} <= tags


def test_original_frozen_package_remains_byte_identical():
    original_root = (
        Path(__file__).resolve().parents[3]
        / "java_ai/validation/product-portfolio-holdout/frozen-v1"
    )
    if not original_root.exists():
        pytest.skip("外部原冻结包不在本机；本地来源副本始终由load_demo校验")
    manifest = json.loads((DEFAULT_DATA.parent / "source-manifest.json").read_text(encoding="utf-8"))
    actual = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in original_root.iterdir() if path.is_file()}
    assert actual == manifest["original_package_files"]


@pytest.fixture
def seeded_session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'seed-test.db'}")
    Base.metadata.create_all(engine)
    with session_factory(engine)() as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize("changed_field,value", [("expected_answer", "用户修订后的参考行为"), ("enabled", False)])
def test_seed_is_idempotent_and_preserves_user_edits(seeded_session, changed_field, value):
    session = seeded_session
    first = seed_demo(session)
    session.commit()
    second = seed_demo(session)
    assert first["status"] == "CREATED" and second["status"] == "ALREADY_PRESENT"
    assert first["dataset_id"] == second["dataset_id"]
    assert session.scalar(select(func.count()).select_from(Dataset)) == 1
    assert session.scalar(select(func.count()).select_from(CaseRow)) == 30
    case = session.scalar(select(CaseRow).where(CaseRow.case_key == "H01"))
    setattr(case, changed_field, value)
    session.commit()
    with pytest.raises(ValueError, match="DEMO_ALREADY_EXISTS_WITH_CHANGES"):
        seed_demo(session)
    assert getattr(session.scalar(select(CaseRow).where(CaseRow.case_key == "H01")), changed_field) == value


def test_seed_refuses_soft_deleted_dataset(seeded_session):
    from agenteval.db import utcnow

    result = seed_demo(seeded_session)
    dataset = seeded_session.get(Dataset, result["dataset_id"])
    dataset.deleted_at = utcnow()
    seeded_session.commit()
    with pytest.raises(ValueError, match="DEMO_ALREADY_EXISTS_WITH_CHANGES"):
        seed_demo(seeded_session)


def test_invalid_last_case_rejects_whole_seed_without_partial_insert(seeded_session, tmp_path):
    from pydantic import ValidationError

    target = tmp_path / "invalid-demo"
    shutil.copytree(DEFAULT_DATA.parent, target)
    asset = json.loads((target / "dataset.json").read_text(encoding="utf-8"))
    asset["cases"][-1]["expected_answer"] = ""
    (target / "dataset.json").write_text(json.dumps(asset, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValidationError):
        seed_demo(seeded_session, target / "dataset.json")
    assert seeded_session.scalar(select(func.count()).select_from(Dataset)) == 0
    assert seeded_session.scalar(select(func.count()).select_from(CaseRow)) == 0
