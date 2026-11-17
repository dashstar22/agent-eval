"""相同冻结条件下的逐指标配对；缺分永远不被当成零。"""

from collections import Counter
from math import fsum, isfinite

from .evaluation_store import load_summary_cases
from .errors import ApiError
from .persistence import EvaluationRun
from .rules import QUALITY_METRICS, RULE_METRICS, tool_statistics
from .schemas import RuleProfile, canonical_id
from .scoring_api import _history, _score_set
from .services import public_row, run_view
from .snapshots import digest
from .statistics import summarize
from .versions import compare_versions, verify_versions

SCORE_FIELDS = "id run_id parent_score_set_id kind judge_snapshot rule_snapshot fingerprint status created_at"
COMPARISON_METRICS = (*QUALITY_METRICS, *RULE_METRICS, "pass_rate", "duration_ms", "tool_success_rate", "input_tokens", "output_tokens", "total_tokens")


def case_content(snapshot):
    return {key: value for key, value in snapshot.items() if key != "revision"}


def resolve_score_set(session, run_id, score_set_id=None):
    if score_set_id is not None:
        try:
            canonical_id(score_set_id)
        except ValueError:
            raise ApiError(422, "INVALID_RESOURCE_ID", "评分集标识须为规范 UUID") from None
    return _score_set(session, run_id, score_set_id)


def comparison_side(session, run_id, score_set_id=None):
    run = _history(session, EvaluationRun, run_id)
    score_set = resolve_score_set(session, run.id, score_set_id)
    cases = load_summary_cases(session, run.id, score_set.id)
    summary = summarize([case["case_key"] for case in run.snapshot["cases"]], cases,
                        run_status=score_set.status if score_set.kind == "RESCORE" else run.status,
                        rule_profile=RuleProfile.model_validate(dict(score_set.rule_snapshot)))
    return run, score_set, cases, {"run": run_view(run), "score_set": public_row(score_set, SCORE_FIELDS), "summary": summary}


def _value(case, metric, profile):
    if case is None:
        return None, "CASE_MISSING"
    if metric == "pass_rate":
        if case.assessment is None or case.assessment.conclusion == "UNDETERMINED":
            return None, "CONCLUSION_UNDETERMINED"
        return int(case.assessment.conclusion == "PASS"), None
    if metric in {*QUALITY_METRICS, *RULE_METRICS}:
        score = next((item for item in case.assessment.metrics if item.metric == metric), None) if case.assessment else None
        if score is None:
            return None, "METRIC_MISSING"
        if score.status not in {"PASS", "FAIL"}:
            return None, "METRIC_" + score.status
        if metric in QUALITY_METRICS:
            value = score.score
            if value is None or not isfinite(value) or value != int(value) or not 1 <= value <= 5:
                return None, "INVALID_METRIC_SCORE"
            return value, None
        return int(score.status == "PASS"), None
    execution = case.execution
    if execution is None:
        return None, "NOT_EXECUTED"
    if metric == "duration_ms":
        return (execution.duration_ms, None) if execution.agent_status == "SUCCEEDED" else (None, "AGENT_NOT_SUCCEEDED")
    if metric == "tool_success_rate":
        value = tool_statistics(execution, profile.tool_sources)["success_rate"]
        return value, None if value is not None else "NO_KNOWN_TOOL_RESULTS"
    usage = summarize([case.case_key], [case], rule_profile=profile)["usage"]["Agent"]
    value = usage[metric]
    # 多轮前置也计入；某一计数器可以完整而其他计数器未知，但未知尝试不能被省掉。
    if usage["applicable_records"] == 0:
        return None, "REPLAY_NO_NEW_USAGE"
    if value is None or usage["known_" + metric + "_records"] != usage["applicable_records"]:
        return None, "USAGE_COUNTER_INCOMPLETE"
    return value, None


def compare_runs(session, left_run_id, right_run_id, left_score_set_id=None, right_score_set_id=None):
    left_run, left_score, left_cases, left = comparison_side(session, left_run_id, left_score_set_id)
    right_run, right_score, right_cases, right = comparison_side(session, right_run_id, right_score_set_id)
    left_snapshots = {item["case_key"]: case_content(item) for item in left_run.snapshot["cases"]}
    right_snapshots = {item["case_key"]: case_content(item) for item in right_run.snapshot["cases"]}
    limits = []
    if digest(left_snapshots) != digest(right_snapshots):
        limits.append("DATA_SNAPSHOT_MISMATCH")
    if digest(dict(left_score.judge_snapshot)) != digest(dict(right_score.judge_snapshot)):
        limits.append("JUDGE_FINGERPRINT_MISMATCH")
    if digest(dict(left_score.rule_snapshot)) != digest(dict(right_score.rule_snapshot)):
        limits.append("RULE_FINGERPRINT_MISMATCH")
    comparable = not limits
    left_map, right_map = {item.case_key: item for item in left_cases}, {item.case_key: item for item in right_cases}
    # IDs come from persisted case rows rather than execution IDs.
    from sqlalchemy import select
    from .persistence import CaseRun
    left_ids = dict(session.execute(select(CaseRun.case_key, CaseRun.id).where(CaseRun.run_id == left_run.id)).all())
    right_ids = dict(session.execute(select(CaseRun.case_key, CaseRun.id).where(CaseRun.run_id == right_run.id)).all())
    keys = sorted(left_snapshots.keys() | right_snapshots.keys())
    pairs, cases = {}, []
    for key in keys:
        content_match = key in left_snapshots and key in right_snapshots and digest(left_snapshots[key]) == digest(right_snapshots[key])
        pairs[key] = content_match
        first, second = left_map.get(key), right_map.get(key)
        cases.append({"case_key": key, "left_case_run_id": left_ids.get(key), "right_case_run_id": right_ids.get(key),
                      "left_conclusion": first.assessment.conclusion if first and first.assessment else "UNDETERMINED",
                      "right_conclusion": second.assessment.conclusion if second and second.assessment else "UNDETERMINED",
                      "content_match": content_match, "reason": None if content_match else "CASE_CONTENT_MISMATCH_OR_MISSING"})
    metrics = []
    left_profile = RuleProfile.model_validate(dict(left_score.rule_snapshot))
    right_profile = RuleProfile.model_validate(dict(right_score.rule_snapshot))
    for metric in COMPARISON_METRICS:
        valid, reasons = [], Counter()
        left_available, right_available = [], []
        for key in keys:
            first, _ = _value(left_map.get(key), metric, left_profile)
            second, _ = _value(right_map.get(key), metric, right_profile)
            if first is not None:
                left_available.append({"case_run_id": left_ids[key], "value": first})
            if second is not None:
                right_available.append({"case_run_id": right_ids[key], "value": second})
        for key in keys:
            if not pairs[key]:
                reasons["CASE_CONTENT_MISMATCH_OR_MISSING"] += 1
                continue
            first, left_reason = _value(left_map.get(key), metric, left_profile)
            second, right_reason = _value(right_map.get(key), metric, right_profile)
            if first is None or second is None:
                if first is None:
                    reasons["LEFT_" + left_reason] += 1
                if second is None:
                    reasons["RIGHT_" + right_reason] += 1
                continue
            valid.append({"case_key": key, "left_case_run_id": left_ids[key], "right_case_run_id": right_ids[key],
                          "left_value": first, "right_value": second, "delta": second - first if comparable else None})
        count = len(valid)
        paired_left_mean = fsum(item["left_value"] for item in valid) / count if count else None
        paired_right_mean = fsum(item["right_value"] for item in valid) / count if count else None
        left_values = [{"case_run_id": item["left_case_run_id"], "value": item["left_value"]} for item in valid] if comparable else left_available
        right_values = [{"case_run_id": item["right_case_run_id"], "value": item["right_value"]} for item in valid] if comparable else right_available
        left_mean = fsum(item["value"] for item in left_values) / len(left_values) if left_values else None
        right_mean = fsum(item["value"] for item in right_values) / len(right_values) if right_values else None
        metrics.append({"metric": metric, "aggregation": "MEAN_OF_PAIRED_CASE_VALUES" if comparable else "MEAN_OF_SIDE_VALID_CASE_VALUES", "paired_count": count, "total_count": len(keys),
                        "mean_scope": "COMMON_VALID_PAIRS" if comparable else "SIDE_VALID_SAMPLES_ONLY",
                        "left_valid_count": len(left_values), "right_valid_count": len(right_values),
                        "left_available_count": len(left_available), "right_available_count": len(right_available),
                        "left_case_run_ids": [item["case_run_id"] for item in left_values],
                        "right_case_run_ids": [item["case_run_id"] for item in right_values],
                        "paired_left_mean": paired_left_mean, "paired_right_mean": paired_right_mean,
                        "paired_coverage": count / len(keys) if keys else None, "left_mean": left_mean, "right_mean": right_mean,
                        "delta": paired_right_mean - paired_left_mean if comparable and count else None,
                        "reason": "CONDITIONS_MISMATCH" if not comparable else "NO_COMMON_VALID_PAIRS" if not count else None,
                        "unpaired_reasons": dict(reasons), "pairs": valid})
    versions = compare_versions(verify_versions(left_run.snapshot, left_cases), verify_versions(right_run.snapshot, right_cases))
    return {"comparable": comparable, "limitations": limits, "left": left, "right": right,
            "metrics": metrics, "cases": cases, "version_comparison": versions}
