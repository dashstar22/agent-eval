"""实验元数据与评分集的安全查询；历史执行只读取冻结内容。"""

from sqlalchemy import func, select

from .errors import ApiError
from .persistence import AgentConfig, CaseEvaluation, Dataset, EvaluationRun, Experiment, ScoreSet
from .privacy import known_secrets, sanitize
from .runtime_api import runtime_run_view
from .services import active, public_row

EXPERIMENT_FIELDS = "id name dataset_id agent_config_id version_label purpose created_at"


def check_experiment_inputs(session, data):
    active(session, Dataset, data.dataset_id)
    config = active(session, AgentConfig, data.agent_config_id)
    if not config.enabled:
        raise ApiError(422, "AGENT_CONFIG_DISABLED", "停用配置不能用于新实验或运行")


def experiment_view(row):
    result = sanitize(public_row(row, EXPERIMENT_FIELDS), known_secrets())
    result["links"] = {"self": f"/api/v1/experiments/{row.id}", "runs": f"/api/v1/experiments/{row.id}/runs"}
    return result


def run_list_view(session, row):
    result = runtime_run_view(session, row)
    result["source_type"] = row.snapshot.get("agent", {}).get("source_type", "UNKNOWN")
    return result


def score_set_view(session, row):
    # 引用名不是密钥值，保留可追溯配置；不解析为真实凭证。
    judge = dict(row.judge_snapshot)
    ref = judge.pop("credential_ref", None)
    result = sanitize(public_row(row, "id run_id parent_score_set_id kind rule_snapshot fingerprint status created_at"), known_secrets({"judge_ref": ref}))
    result["judge_snapshot"] = sanitize(judge, known_secrets({"judge_ref": ref})) | {"credential_ref": ref}
    run = session.get(EvaluationRun, row.run_id)
    evaluated = session.scalar(select(func.count()).select_from(CaseEvaluation).where(CaseEvaluation.score_set_id == row.id))
    result["progress"] = {"total": run.total_cases, "evaluated": evaluated,
                          "remaining": run.total_cases - evaluated, "complete": row.status == "COMPLETED"}
    result["execution_budget"] = run.snapshot["execution_budget"]
    result["agent_usage_reused"] = row.kind == "RESCORE"
    return result
