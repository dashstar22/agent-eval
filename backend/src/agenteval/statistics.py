"""固定冻结分母的报告统计；未知用量不补零，Agent/Judge 分账。"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from math import fsum, isfinite
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from .evaluation_models import SummaryCase
from .models import ExecutionResult, Record, Usage
from .rules import QUALITY_METRICS, tool_statistics
from .schemas import RuleProfile


class Price(Record):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    model: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    unit_tokens: int = Field(ge=1)
    input_per_unit: float = Field(ge=0)
    output_per_unit: float = Field(ge=0)
    effective_source: str = Field(min_length=1)
    effective_from: str = Field(min_length=1)

    @field_validator("model", "currency", "effective_source", "effective_from")
    @classmethod
    def nonblank_metadata(cls, value):
        if not value.strip():
            raise ValueError("PRICE_METADATA_MUST_NOT_BE_BLANK")
        return value.strip()


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _execution_chain(execution: ExecutionResult) -> list[ExecutionResult]:
    """显式前置轮次可能嵌套保留前史，只按 execution_id 计量一次。"""
    seen: set[str] = set()
    result: list[ExecutionResult] = []

    def collect(item: ExecutionResult):
        if item.execution_id in seen:
            return
        seen.add(item.execution_id)
        for prerequisite in item.prerequisites:
            collect(ExecutionResult.model_validate(prerequisite))
        result.append(item)

    collect(execution)
    return result


def _usage_rows(usage: Usage, model: str | None = None) -> list[dict[str, Any]]:
    if usage.status == "NOT_REQUESTED":
        return [{"availability": "NOT_REQUESTED", "model": model, "input_tokens": None, "output_tokens": None, "total_tokens": None}]
    if usage.attempts:
        # 尝试列表是逐次依据，不能再加上同一列表的 aggregate。
        return [{"availability": attempt.availability if usage.status == "COMPLETE" else "PARTIAL" if attempt.availability == "COMPLETE" else attempt.availability, "model": attempt.model_id or model,
                 "input_tokens": attempt.input_tokens, "output_tokens": attempt.output_tokens,
                 "total_tokens": attempt.total_tokens} for attempt in usage.attempts]
    return [{"availability": usage.status, "model": model, "input_tokens": usage.input_tokens,
             "output_tokens": usage.output_tokens, "total_tokens": usage.total_tokens}]


def summarize_usage(rows: list[dict[str, Any]], prices: Sequence[Price] = ()) -> dict[str, Any]:
    applicable = [row for row in rows if row["availability"] != "NOT_REQUESTED"]
    result: dict[str, Any] = {"usage_records": len(rows), "applicable_records": len(applicable), "not_requested_records": len(rows) - len(applicable)}
    for counter in ("input_tokens", "output_tokens", "total_tokens"):
        values = [row[counter] for row in applicable if row[counter] is not None]
        result[counter] = sum(values) if values else None
        result["known_" + counter + "_records"] = len(values)
    complete = bool(applicable) and all(row["availability"] == "COMPLETE" and all(row[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens")) for row in applicable)
    result["availability"] = "COMPLETE" if complete else "PARTIAL" if any(result[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens")) else "NOT_REQUESTED" if rows and not applicable else "UNAVAILABLE"
    result["known_part_only"] = result["availability"] == "PARTIAL"
    by_model: dict[str, Price] = {}
    for price in prices:
        if price.model in by_model:
            raise ValueError("PRICE_MODEL_MUST_BE_UNIQUE")
        by_model[price.model] = price
    known_costs: dict[str, list[float]] = defaultdict(list)
    priced_records, sources = 0, []
    for row in applicable:
        price = by_model.get(row["model"])
        if price is None or row["input_tokens"] is None or row["output_tokens"] is None:
            continue
        known_costs[price.currency].append((row["input_tokens"] * price.input_per_unit + row["output_tokens"] * price.output_per_unit) / price.unit_tokens)
        priced_records += 1
        if price.model_dump() not in sources:
            sources.append(price.model_dump())
    cost_by_currency = {currency: fsum(costs) for currency, costs in known_costs.items()}
    full_cost = bool(applicable) and priced_records == len(applicable) and complete and len(cost_by_currency) == 1
    result.update({
        "cost": next(iter(cost_by_currency.values())) if full_cost else None,
        "cost_currency": next(iter(cost_by_currency)) if full_cost else None,
        "known_cost_by_currency": cost_by_currency,
        "priced_records": priced_records, "cost_known_part_only": bool(cost_by_currency) and not full_cost,
        "price_configuration": sources,
        "cost_reason": None if full_cost else "NO_PRICE_CONFIGURATION" if not prices else "INCOMPLETE_USAGE_OR_UNPRICED_MODEL" if len(cost_by_currency) <= 1 else "MULTIPLE_CURRENCIES",
    })
    return result


def summarize(frozen_case_keys: Iterable[str], cases: list[SummaryCase], *, run_status: str = "COMPLETED", rule_profile: RuleProfile | Mapping[str, Any] | None = None, prices: Sequence[Price | Mapping[str, Any]] = ()) -> dict[str, Any]:
    keys = list(frozen_case_keys)
    if len(set(keys)) != len(keys):
        raise ValueError("FROZEN_CASE_KEYS_MUST_BE_UNIQUE")
    if len({case.case_key for case in cases}) != len(cases):
        raise ValueError("SUMMARY_CASE_KEYS_MUST_BE_UNIQUE")
    if not {case.case_key for case in cases} <= set(keys):
        raise ValueError("SUMMARY_CASE_NOT_IN_FROZEN_PLAN")
    profile = RuleProfile() if rule_profile is None else rule_profile if isinstance(rule_profile, RuleProfile) else RuleProfile.model_validate(dict(rule_profile))
    price_records = [price if isinstance(price, Price) else Price.model_validate(dict(price)) for price in prices]
    by_key = {case.case_key: case for case in cases}
    n = len(keys)
    counts = Counter({"PASS": 0, "FAIL": 0, "UNDETERMINED": 0})
    quality: dict[str, list[float]] = {metric: [] for metric in QUALITY_METRICS}
    metric_statuses: dict[str, Counter] = defaultdict(Counter)
    successful_durations: list[int] = []
    agent_statuses: Counter = Counter()
    source_types: Counter = Counter()
    tools_counts: Counter = Counter()
    trajectory_counts: Counter = Counter()
    agent_usage, judge_usage = [], []
    model_verified_count = 0
    judge_statuses: Counter = Counter()
    judge_sources: Counter = Counter()
    real_judge_success_count = 0
    for key in keys:
        item = by_key.get(key)
        assessment = None if item is None else item.assessment
        counts[assessment.conclusion if assessment is not None else "UNDETERMINED"] += 1
        if assessment is not None:
            seen_metrics: set[str] = set()
            for metric in assessment.metrics:
                if metric.metric in seen_metrics:
                    raise ValueError("DUPLICATE_CASE_METRIC")
                seen_metrics.add(metric.metric)
                metric_statuses[metric.metric][metric.status] += 1
                if metric.metric in quality and metric.status in {"PASS", "FAIL"} and metric.score is not None:
                    if not isfinite(metric.score) or not 1 <= metric.score <= 5 or metric.score != int(metric.score):
                        raise ValueError("INVALID_QUALITY_SCORE_IN_SUMMARY")
                    quality[metric.metric].append(metric.score)
        execution = None if item is None else item.execution
        if execution is None:
            agent_statuses["NOT_EXECUTED"] += 1
        else:
            agent_statuses[execution.agent_status] += 1
            source_types[execution.source_type] += 1
            model_verified_count += int(execution.model_execution_verified)
            if execution.agent_status == "SUCCEEDED":
                successful_durations.append(execution.duration_ms)
            for turn in _execution_chain(execution):
                usage = Usage(status="NOT_REQUESTED", reason="REPLAY_NO_NEW_MODEL_REQUEST") if turn.replayed else turn.usage
                agent_usage.extend(_usage_rows(usage, turn.observed_version.model_id))
            stats = tool_statistics(execution, profile.tool_sources)
            trajectory_counts[stats["trajectory_completeness"]] += 1
            for counter in ("normal_completion", "business_refusal", "technical_failure", "unknown", "unknown_source", "event_count", "denominator"):
                tools_counts[counter] += stats[counter]
        judge = None if item is None else item.judge
        judge_statuses["NOT_EVALUATED" if judge is None else judge.status] += 1
        if judge is not None:
            successful_sources = []
            for attempt in judge.attempts:
                judge_usage.extend(_usage_rows(attempt.usage))
                request_data = attempt.request.get("data", attempt.request)
                source = request_data.get("source_type", "UNKNOWN") if isinstance(request_data, dict) else "UNKNOWN"
                judge_sources[source] += 1
                if attempt.status == "SUCCEEDED":
                    successful_sources.append(source)
            if judge.status == "SUCCEEDED" and successful_sources and successful_sources[-1] == "REAL_EXTERNAL":
                real_judge_success_count += 1
    p, f, u = counts["PASS"], counts["FAIL"], counts["UNDETERMINED"]
    executed_count = n - agent_statuses["NOT_EXECUTED"]
    denominator = tools_counts["denominator"]
    # 未执行用例同样保留在轨迹覆盖范围中。
    trajectory_counts["NOT_EXECUTED"] = agent_statuses["NOT_EXECUTED"]
    return {
        "N": n, "P": p, "F": f, "U": u, "run_status": run_status,
        "provisional": run_status in {"FROZEN", "QUEUED", "RUNNING", "PENDING"},
        "interrupted": run_status == "INTERRUPTED",
        "effective_pass_rate": _ratio(p, p + f), "determinate_coverage": _ratio(p + f, n),
        "metric_averages": {metric: {"mean": fsum(values) / len(values) if values else None, "valid_count": len(values), "total_count": n} for metric, values in quality.items()},
        "metric_status_counts": {metric: {status: statuses[status] for status in ("PASS", "FAIL", "NA", "ERROR")} for metric, statuses in metric_statuses.items()},
        "agent": {"status_counts": dict(agent_statuses), "executed_count": executed_count, "total_count": n,
                  "success_duration_mean_ms": fsum(successful_durations) / len(successful_durations) if successful_durations else None,
                  "success_duration_valid_count": len(successful_durations), "timeout_count": agent_statuses["TIMEOUT"],
                  "timeout_rate": _ratio(agent_statuses["TIMEOUT"], executed_count), "timeout_denominator": executed_count},
        "judge": {"status_counts": dict(judge_statuses)},
        "tools": {counter: tools_counts[counter] for counter in ("normal_completion", "business_refusal", "technical_failure", "unknown", "unknown_source", "event_count", "denominator")} | {
            "success_rate": _ratio(tools_counts["normal_completion"], denominator), "trajectory_counts": dict(trajectory_counts),
            "sources": profile.tool_sources, "reason": None if denominator else "NO_KNOWN_NORMAL_OR_TECHNICAL_RESULTS"},
        "usage": {"Agent": summarize_usage(agent_usage, price_records), "Judge": summarize_usage(judge_usage, price_records)},
        "source_scope": {"counts": dict(source_types), "contains_simulation": bool(source_types["MOCK"] or source_types["PROTOCOL_DOUBLE"] or judge_sources["PROTOCOL_DOUBLE"]),
                         "model_verified_count": model_verified_count, "judge_attempt_source_counts": dict(judge_sources),
                         "real_judge_success_count": real_judge_success_count,
                         "verified_real_agent_execution": bool(executed_count) and model_verified_count == executed_count and not source_types["MOCK"] and not source_types["PROTOCOL_DOUBLE"],
                         "real_quality_evidence": bool(executed_count) and model_verified_count == executed_count and real_judge_success_count == executed_count and not source_types["MOCK"] and not source_types["PROTOCOL_DOUBLE"] and not judge_sources["PROTOCOL_DOUBLE"] and not judge_sources["UNKNOWN"]},
    }
