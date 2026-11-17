"""SQLite快速回归及显式独立MySQL schema上的真实约束验证。"""

import os
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from agenteval.db import Base, make_engine
from agenteval.persistence import (
    AgentConfig, AgentTrace, BadcaseAnnotation, CaseEvaluation, CaseRun,
    Dataset, EvaluationRun, EvaluationScore, Experiment, JudgeAttempt,
    ScoreSet, TestCase,
)


def fixed_migrations():
    migrations = []
    for migration in sorted((Path(__file__).parents[1] / "alembic/versions").glob("*.py")):
        spec = importlib.util.spec_from_file_location(migration.stem, migration)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        migrations.append(module)
    return migrations


def test_fixed_migration_matches_models_and_seeds_control():
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    engine = make_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for module in fixed_migrations():
                module.upgrade()
        assert compare_metadata(context, Base.metadata) == []
        assert connection.scalar(text("SELECT id FROM runtime_control")) == 1
    engine.dispose()


def test_0002_upgrade_preserves_legacy_rows_and_frozen_fingerprints():
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import MetaData, Table

    from agenteval.schemas import CaseCreate
    from agenteval.services import case_content
    from agenteval.snapshots import digest

    migrations = {module.revision: module for module in fixed_migrations()}
    engine = make_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migrations["20261116_0001"].upgrade()
    assert "tool_assertions" not in {column["name"] for column in inspect(engine).get_columns("test_cases")}
    legacy_cases = Table("test_cases", MetaData(), autoload_with=engine)
    old_content = CaseCreate(case_key="LEGACY", question="旧问题", expected_answer="旧参考行为",
                             expected_tools=["search"], tags=["历史合成"],
                             preconditions={"prior_turns": ["旧前置轮次"]}, source={"fixture": "0001"},
                             license="CC0-1.0", seen_regression=True).model_dump(exclude={"tool_assertions"})
    old_case_fingerprint = digest(old_content)
    snapshot = {"schema_version": "agenteval-snapshot-v1", "cases": [old_content | {"revision": 7}],
                "judge": {"model": "legacy-judge"}, "rules": {"version": "legacy-rules"}}
    with Session(engine) as session, session.begin():
        dataset = Dataset(name="旧数据集", revision=4)
        config = AgentConfig(name="旧连接", adapter_type="MOCK", base_url="http://127.0.0.1", source_type="MOCK")
        session.add_all([dataset, config])
        session.flush()
        stamp = datetime(2026, 11, 16, 1, 2, 3, 123456, tzinfo=timezone.utc)
        saved_case_id = session.execute(legacy_cases.insert().values(
            dataset_id=dataset.id, **old_content, revision=7, created_at=stamp, updated_at=stamp,
        )).inserted_primary_key[0]
        experiment = Experiment(name="旧实验", dataset_id=dataset.id, agent_config_id=config.id)
        session.add(experiment)
        session.flush()
        run = EvaluationRun(experiment_id=experiment.id, request_id=str(uuid4()), status="COMPLETED",
                            snapshot=snapshot, snapshot_sha256=digest(snapshot), total_cases=1)
        session.add(run)
        session.flush()
        case_run = CaseRun(run_id=run.id, case_key="LEGACY", case_snapshot=snapshot["cases"][0],
                           phase="EVALUATED", agent_status="SUCCEEDED", answer="旧回答",
                           raw_agent={"fixture": "legacy-evidence"}, usage={"total_tokens": None})
        score = ScoreSet(run_id=run.id, status="COMPLETED", judge_snapshot=snapshot["judge"],
                         rule_snapshot=snapshot["rules"], fingerprint="b" * 64)
        session.add_all([case_run, score])
        session.flush()
        session.add_all([
            CaseEvaluation(run_id=run.id, score_set_id=score.id, case_run_id=case_run.id,
                           status="SUCCEEDED", conclusion="PASS", completed_at=stamp),
            EvaluationScore(run_id=run.id, score_set_id=score.id, case_run_id=case_run.id,
                            metric="correctness", status="PASS", score=5, reason="旧评分理由", evidence={"fixture": "0001"}),
            BadcaseAnnotation(run_id=run.id, score_set_id=score.id, case_run_id=case_run.id, note="旧人工备注"),
        ])
        run_id, case_run_id = run.id, case_run.id
    with engine.connect() as connection:
        old_tables = inspect(connection).get_table_names()
        # 原始数据库值也必须相等，避免反序列化后的等价对象掩盖历史 JSON 重写。
        before = {table: [dict(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY id').mappings()]
                  for table in old_tables}
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migrations["20261117_0002"].upgrade()
        for table, rows in before.items():
            after = [dict(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY id').mappings()]
            if table == "test_cases":
                assert all(row.pop("tool_assertions") is None for row in after)
            assert after == rows, table
        assert compare_metadata(context, Base.metadata) == []
    with Session(engine) as session:
        case = session.get(TestCase, saved_case_id)
        assert case.tool_assertions is None
        assert case_content(case) == old_content and digest(case_content(case)) == old_case_fingerprint
        saved_run = session.get(EvaluationRun, run_id)
        assert saved_run.snapshot == snapshot and saved_run.snapshot_sha256 == digest(snapshot)
        assert session.get(CaseRun, case_run_id).case_snapshot == snapshot["cases"][0]
    engine.dispose()


def test_empty_tool_assertions_preserve_case_content_fingerprint_after_reload():
    from agenteval.services import case_content
    from agenteval.snapshots import digest

    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, _, case, *_ = make_run(session)
        original = case_content(case)
        original_fingerprint = digest(original)
        assert "tool_assertions" not in original
        for assertions in (None, {}, {"sequence": None, "max_calls": {}}):
            case.tool_assertions = assertions
            session.flush()
            session.expire(case)
            assert case.tool_assertions == assertions
            assert case_content(case) == original and digest(case_content(case)) == original_fingerprint
        case.tool_assertions = {"sequence": ["search"], "max_calls": {"search": 1}}
        session.flush()
        session.expire(case)
        assert case_content(case)["tool_assertions"] == case.tool_assertions
        assert digest(case_content(case)) != original_fingerprint
    engine.dispose()


_URLS = ["sqlite+pysqlite:///:memory:"]
if os.environ.get("AGENTEVAL_TEST_DATABASE_URL"):
    _URLS.append(os.environ["AGENTEVAL_TEST_DATABASE_URL"])


@pytest.fixture(params=_URLS, ids=lambda url: "mysql" if url.startswith("mysql") else "sqlite")
def store(request):
    engine = make_engine(request.param)
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
    else:
        assert engine.url.database.startswith("agenteval_")
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(connection, expire_on_commit=False, join_transaction_mode="create_savepoint") as session:
            yield session
        transaction.rollback()
    engine.dispose()


def make_run(session):
    dataset = Dataset(name="约束验证")
    config = AgentConfig(name="模拟", adapter_type="mock", base_url="http://127.0.0.1", source_type="MOCK")
    session.add_all([dataset, config])
    session.flush()
    case = TestCase(dataset_id=dataset.id, case_key="C001", question="测试问题", expected_answer="预期行为")
    experiment = Experiment(name="约束实验", dataset_id=dataset.id, agent_config_id=config.id)
    session.add_all([case, experiment])
    session.flush()
    run = EvaluationRun(experiment_id=experiment.id, request_id=str(uuid4()), snapshot={"cases": [{"question": "原问题"}]}, snapshot_sha256="a" * 64, total_cases=1)
    session.add(run)
    session.flush()
    case_run = CaseRun(run_id=run.id, case_key="C001", case_snapshot={"question": "原问题", "tags": ["合成"]})
    score_set = ScoreSet(run_id=run.id, judge_snapshot={"model": "judge", "rubric": {"max": 5}}, rule_snapshot={"threshold": 4}, fingerprint="b" * 64)
    session.add_all([case_run, score_set])
    session.flush()
    return dataset, config, case, experiment, run, case_run, score_set


def test_schema_entities_indexes_and_utc(store):
    tables = inspect(store.bind).get_table_names()
    assert set(Base.metadata.tables).issubset(tables)
    assert len(Base.metadata.tables) == 17
    assert {index["name"] for index in inspect(store.bind).get_indexes("case_runs")} >= {"ix_case_run_phase"}
    dataset, *_ = make_run(store)
    store.expire(dataset)
    assert dataset.created_at.tzinfo == timezone.utc
    if store.bind.dialect.name == "mysql":
        columns = {column["name"]: column for column in inspect(store.bind).get_columns("datasets")}
        assert columns["created_at"]["type"].fsp == 6
        score_columns = {column["name"]: column for column in inspect(store.bind).get_columns("score_sets")}
        assert score_columns["original_run_key"]["computed"]["persisted"] is True


def test_original_unique_multiple_rescores(store):
    *_, run, case_run, original = make_run(store)
    for _ in range(2):
        store.add(ScoreSet(run_id=run.id, parent_score_set_id=original.id, kind="RESCORE", judge_snapshot={}, rule_snapshot={}, fingerprint="c" * 64))
        store.flush()
    assert len(store.scalars(select(ScoreSet).where(ScoreSet.run_id == run.id)).all()) == 3
    with pytest.raises(IntegrityError), store.begin_nested():
        store.add(ScoreSet(run_id=run.id, kind="ORIGINAL", judge_snapshot={}, rule_snapshot={}, fingerprint="d" * 64))
        store.flush()


@pytest.mark.parametrize("model,extra", [
    (CaseEvaluation, {}),
    (JudgeAttempt, {"attempt": 1}),
    (EvaluationScore, {"metric": "correctness", "status": "PASS"}),
    (BadcaseAnnotation, {}),
])
def test_every_scoring_table_rejects_cross_run_case(store, model, extra):
    *_, run_a, case_a, score_a = make_run(store)
    *_, run_b, case_b, score_b = make_run(store)
    with pytest.raises(IntegrityError), store.begin_nested():
        store.add(model(run_id=run_a.id, score_set_id=score_a.id, case_run_id=case_b.id, **extra))
        store.flush()
    with pytest.raises(IntegrityError), store.begin_nested():
        store.add(model(run_id=run_a.id, score_set_id=score_b.id, case_run_id=case_a.id, **extra))
        store.flush()


def test_parent_score_set_must_belong_to_same_run(store):
    *_, run_a, case_a, original_a = make_run(store)
    *_, run_b, case_b, original_b = make_run(store)
    with pytest.raises(IntegrityError), store.begin_nested():
        store.add(ScoreSet(run_id=run_b.id, parent_score_set_id=original_a.id, kind="RESCORE", judge_snapshot={}, rule_snapshot={}, fingerprint="e" * 64))
        store.flush()


def test_snapshot_replacement_and_nested_mutation_rejected(store):
    *_, run, case_run, score_set = make_run(store)
    with pytest.raises(TypeError, match="IMMUTABLE_SNAPSHOT"):
        run.snapshot["cases"][0]["question"] = "篡改"
    with pytest.raises(TypeError, match="IMMUTABLE_SNAPSHOT"):
        case_run.case_snapshot["tags"].append("篡改")
    with pytest.raises(TypeError, match="IMMUTABLE_SNAPSHOT"):
        score_set.judge_snapshot["rubric"]["max"] = 1
    for record, attribute in ((run, "snapshot"), (case_run, "case_snapshot"), (score_set, "rule_snapshot")):
        with pytest.raises(ValueError, match="IMMUTABLE_SNAPSHOT"), store.begin_nested():
            setattr(record, attribute, {"tampered": True})
            store.flush()
        store.expire(record)
    store.expire_all()
    with pytest.raises(TypeError, match="IMMUTABLE_SNAPSHOT"):
        run.snapshot["cases"].append({})


def test_source_soft_deletion_preserves_history_and_fk_restrict(store):
    dataset, config, case, experiment, run, case_run, score_set = make_run(store)
    store.add(EvaluationScore(run_id=run.id, score_set_id=score_set.id, case_run_id=case_run.id, metric="correctness", status="PASS", score=4))
    store.flush()
    for source in (dataset, config, case, experiment):
        source.deleted_at = datetime.now(timezone.utc)
    case.question = "修改后的问题"
    config.base_url = "http://127.0.0.1/changed"
    store.flush()
    store.expire_all()
    assert run.snapshot["cases"][0]["question"] == "原问题"
    assert case_run.case_snapshot["question"] == "原问题"
    assert store.scalar(select(EvaluationScore.score).where(EvaluationScore.score_set_id == score_set.id)) == 4
    for model, source_id in ((Dataset, dataset.id), (AgentConfig, config.id), (Experiment, experiment.id)):
        with pytest.raises(IntegrityError), store.begin_nested():
            store.execute(delete(model).where(model.id == source_id))
    with pytest.raises(IntegrityError), store.begin_nested():
        store.execute(delete(EvaluationRun).where(EvaluationRun.id == run.id))


def test_stable_case_key_uniqueness_includes_deleted_cases(store):
    dataset, config, case, *_ = make_run(store)
    case.deleted_at = datetime.now(timezone.utc)
    store.flush()
    with pytest.raises(IntegrityError), store.begin_nested():
        store.add(TestCase(dataset_id=dataset.id, case_key=case.case_key, question="不复用旧身份", expected_answer="新建用例键"))
        store.flush()


def test_naive_timestamp_rejected(store):
    with pytest.raises(StatementError, match="UTC_TIMESTAMP_REQUIRES_TIMEZONE"), store.begin_nested():
        store.add(Dataset(name="无时区", created_at=datetime(2026, 11, 16)))
        store.flush()
