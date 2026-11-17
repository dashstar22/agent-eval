"""六项程序规则与后端结论；缺失证据和失败保持不同状态。"""

from collections.abc import Mapping, Sequence
from typing import Any

from .evaluation_models import CaseAssessment, JudgeResult, MetricResult
from .models import ExecutionResult
from .schemas import DEFAULT_REQUIRED, METRICS, RuleProfile, ToolAssertions

RULE_METRICS = ("execution_completed", "answer_nonempty", "expected_tools", "tool_success", "response_time", "tool_names")
QUALITY_METRICS = ("correctness", "relevance", "task_completion", "faithfulness")
RULE_DEPENDENCIES = {
    "execution_completed": ["agent_status"], "answer_nonempty": ["answer"],
    "expected_tools": ["expected_tools", "tools", "tools_completeness", "tool_sources"],
    "tool_success": ["tools", "tools_completeness", "tool_sources", "tool_status_mapping"],
    "response_time": ["agent_status", "duration_ms", "response_time_ms"],
    "tool_names": ["expected_tools", "tool_match_mode", "tools", "tools_completeness", "tool_sources"],
    "tool_order": ["tool_assertions.sequence", "tools", "tools_completeness", "tool_sources"],
    "tool_call_count": ["tool_assertions.max_calls", "tools", "tools_completeness", "tool_sources"],
}
TOOL_STATUS_VERSION = "agenteval-tool-status-v1"
NORMAL_STATUSES = frozenset({"ORDER_FOUND", "HIT", "NO_EVIDENCE", "SUCCEEDED", "SUCCESS", "COMPLETED", "ORDER_NOT_FOUND", "TICKET_CREATED", "DRAFT_CREATED"})
BUSINESS_REFUSAL_STATUSES = frozenset({"PAGE_CONFIRMATION_REQUIRED", "ACCESS_DENIED", "AUTHORIZATION_DENIED", "FORBIDDEN", "UNAUTHORIZED", "OWNERSHIP_DENIED", "NOT_AUTHORIZED", "RESOURCE_NOT_OWNED"})
TECHNICAL_FAILURE_STATUSES = frozenset({"RETRIEVAL_UNAVAILABLE", "TOOL_TIMEOUT", "TIMEOUT", "TOOL_ERROR", "TECHNICAL_FAILURE", "DATABASE_UNAVAILABLE", "INTERNAL_ERROR", "WRITE_RESULT_UNKNOWN"})


def _profile(value: RuleProfile | Mapping[str, Any]) -> RuleProfile:
    return value if isinstance(value, RuleProfile) else RuleProfile.model_validate(dict(value))


def required_metrics(case_snapshot: Mapping[str, Any]) -> set[str]:
    required = case_snapshot.get("required_metrics", DEFAULT_REQUIRED)
    if not isinstance(required, list) or not required or len(set(required)) != len(required) or not set(required) <= METRICS:
        raise ValueError("REQUIRED_METRICS_MUST_BE_NONEMPTY_KNOWN_UNIQUE")
    return set(required)


def tool_status_category(status: str) -> str:
    """HTTP 成功及未列明字符串都不能证明工具技术执行成功。"""
    if status in NORMAL_STATUSES:
        return "NORMAL_COMPLETION"
    if status in BUSINESS_REFUSAL_STATUSES:
        return "BUSINESS_REFUSAL"
    if status in TECHNICAL_FAILURE_STATUSES:
        return "TECHNICAL_FAILURE"
    return "UNKNOWN"


def tool_statistics(execution: ExecutionResult | None, tool_sources: Sequence[str] = ("CHAT_TOOL",)) -> dict[str, Any]:
    events = [] if execution is None or execution.tools is None else [event for event in execution.tools if event.kind == "TOOL" and event.source in tool_sources]
    counts = {"NORMAL_COMPLETION": 0, "BUSINESS_REFUSAL": 0, "TECHNICAL_FAILURE": 0, "UNKNOWN": 0}
    for event in events:
        counts[tool_status_category(event.status)] += 1
    denominator = counts["NORMAL_COMPLETION"] + counts["TECHNICAL_FAILURE"]
    unknown_source = 0 if execution is None or execution.tools is None else sum(event.kind == "TOOL" and event.source == "UNKNOWN" for event in execution.tools)
    completeness = "UNAVAILABLE" if execution is None else execution.tools_completeness
    return {
        "status_mapping_version": TOOL_STATUS_VERSION,
        "sources": list(tool_sources), "normal_completion": counts["NORMAL_COMPLETION"],
        "business_refusal": counts["BUSINESS_REFUSAL"], "technical_failure": counts["TECHNICAL_FAILURE"],
        "unknown": counts["UNKNOWN"], "unknown_source": unknown_source,
        "event_count": len(events), "denominator": denominator,
        "success_rate": counts["NORMAL_COMPLETION"] / denominator if denominator else None,
        "reported_trajectory_completeness": completeness,
        "trajectory_completeness": "PARTIAL" if completeness == "COMPLETE" and unknown_source else completeness,
        "reason": None if denominator else "NO_KNOWN_NORMAL_OR_TECHNICAL_RESULTS",
    }


def _tool_match(expected: set[str], actual: set[str], *, complete: bool, mode: str) -> tuple[str, str]:
    missing, extra = expected - actual, actual - expected
    if mode == "exact" and extra:
        return "FAIL", "OBSERVED_UNEXPECTED_TOOLS"
    if not missing and mode == "contains":
        return "PASS", "ALL_EXPECTED_TOOLS_OBSERVED"
    if not complete:
        return "NA", "TOOL_TRAJECTORY_INCOMPLETE"
    if missing:
        return "FAIL", "EXPECTED_TOOLS_NOT_CALLED"
    return "PASS", "TOOL_NAME_SETS_MATCH"


def evaluate_rules(case_snapshot: Mapping[str, Any], execution: ExecutionResult | None, rule_profile: RuleProfile | Mapping[str, Any]) -> list[MetricResult]:
    profile, required = _profile(rule_profile), required_metrics(case_snapshot)
    expected_raw = case_snapshot.get("expected_tools")
    mode = case_snapshot.get("tool_match_mode", "contains")
    if mode not in {"contains", "exact"} or (expected_raw == [] and mode != "exact"):
        raise ValueError("INVALID_TOOL_MATCH_CONFIGURATION")
    if expected_raw is not None and (not isinstance(expected_raw, list) or any(not isinstance(name, str) or not name.strip() for name in expected_raw) or len(set(expected_raw)) != len(expected_raw)):
        raise ValueError("INVALID_EXPECTED_TOOLS")
    base = {"rule_version": profile.version}

    def metric(name, status, reason, evidence=None):
        return MetricResult(metric=name, status=status, required=name in required, reason=reason, evidence=base | {"dependencies": RULE_DEPENDENCIES[name]} | (evidence or {}))

    if execution is None:
        return [metric(name, "NA", "AGENT_NOT_EXECUTED", {"answer_available": False, "context_scope": "UNAVAILABLE", "context_completeness": "UNAVAILABLE", "context_count": 0}) for name in RULE_METRICS] + evaluate_tool_assertions(case_snapshot, execution, profile)
    facts = {
        "agent_status": execution.agent_status, "execution_id": execution.execution_id,
        "source_type": execution.source_type, "answer_available": execution.answer is not None,
        "context_scope": execution.context_scope, "context_completeness": execution.context_completeness,
        "context_count": len(execution.contexts or []),
        "citation_support_only": execution.context_scope == "CITATION_SUBSET",
    }
    execution_status = "PASS" if execution.agent_status == "SUCCEEDED" else ("NA" if execution.agent_status == "RESULT_UNKNOWN" else "FAIL")
    results = [metric("execution_completed", execution_status, "AGENT_EXECUTION_" + execution.agent_status, facts)]
    if execution.agent_status == "RESULT_UNKNOWN" and execution.answer is None:
        answer_status, answer_reason = "NA", "ANSWER_UNAVAILABLE_RESULT_UNKNOWN"
    elif execution.answer is not None and execution.answer.strip():
        answer_status, answer_reason = "PASS", "ANSWER_NONEMPTY"
    else:
        answer_status, answer_reason = "FAIL", "ANSWER_EMPTY"
    results.append(metric("answer_nonempty", answer_status, answer_reason, {"answer_available": execution.answer is not None, "non_whitespace_length": len((execution.answer or "").strip()) if execution.answer is not None else None}))
    events = [] if execution.tools is None else [event for event in execution.tools if event.kind == "TOOL" and event.source in profile.tool_sources]
    actual = {event.name for event in events}
    complete = execution.tools is not None and execution.tools_completeness == "COMPLETE" and not any(event.kind == "TOOL" and event.source == "UNKNOWN" for event in (execution.tools or []))
    tool_evidence = {
        "expected_names": expected_raw, "actual_names": sorted(actual),
        "actual_order": [event.name for event in events], "sources": profile.tool_sources,
        "trajectory_completeness": execution.tools_completeness, "name_set_evaluation_only": True,
    }
    if expected_raw is None:
        expected_status, expected_reason = names_status, names_reason = "NA", "EXPECTED_TOOLS_NOT_CONFIGURED"
    elif execution.tools is None:
        expected_status, expected_reason = names_status, names_reason = "NA", "TOOL_TRAJECTORY_UNAVAILABLE"
    else:
        expected = set(expected_raw)
        # “预期工具出现”只检查包含；空集合表示禁止调用时采用 exact。
        expected_status, expected_reason = _tool_match(expected, actual, complete=complete, mode="exact" if not expected else "contains")
        names_status, names_reason = _tool_match(expected, actual, complete=complete, mode=mode)
    results.append(metric("expected_tools", expected_status, expected_reason, tool_evidence | {"match_mode": "exact" if expected_raw == [] else "contains"}))
    stats = tool_statistics(execution, profile.tool_sources)
    if stats["technical_failure"]:
        success_status, success_reason = "FAIL", "TECHNICAL_TOOL_FAILURE_OBSERVED"
    elif not complete:
        success_status, success_reason = "NA", "TOOL_TRAJECTORY_INCOMPLETE"
    elif stats["unknown"]:
        success_status, success_reason = "NA", "TOOL_RESULT_STATUS_UNKNOWN"
    elif not events:
        success_status, success_reason = "NA", "NO_APPLICABLE_TOOL_CALLS"
    else:
        success_status, success_reason = "PASS", "TOOLS_COMPLETED_OR_LEGALLY_REFUSED"
    results.append(metric("tool_success", success_status, success_reason, stats))
    time_status = "NA" if execution.agent_status != "SUCCEEDED" else ("PASS" if execution.duration_ms <= profile.response_time_ms else "FAIL")
    results.append(metric("response_time", time_status, "AGENT_NOT_SUCCEEDED" if time_status == "NA" else "RESPONSE_WITHIN_THRESHOLD" if time_status == "PASS" else "RESPONSE_EXCEEDS_THRESHOLD", {"actual_ms": execution.duration_ms, "threshold_ms": profile.response_time_ms}))
    results.append(metric("tool_names", names_status, names_reason, tool_evidence | {"match_mode": mode}))
    return results + evaluate_tool_assertions(case_snapshot, execution, profile)


def evaluate_tool_assertions(case_snapshot, execution, profile):
    """仅评价明确配置的断言；旧快照不追加指标或改变原结论。"""
    config = ToolAssertions.model_validate(dict(case_snapshot.get("tool_assertions") or {}))
    required = required_metrics(case_snapshot)
    if not config.sequence and not config.max_calls:
        return []
    events = [] if execution is None or execution.tools is None else [
        event for event in execution.tools if event.kind == "TOOL" and event.source in profile.tool_sources
    ]
    complete = execution is not None and execution.tools is not None and execution.tools_completeness == "COMPLETE" and not any(
        event.kind == "TOOL" and event.source == "UNKNOWN" for event in execution.tools
    )
    evidence = {
        "rule_version": profile.version, "assertion_version": "agenteval-tool-assertions-v1",
        "sources": profile.tool_sources, "trajectory_complete": complete,
        "events": [{"event_id": event.event_id, "sequence": event.sequence, "name": event.name} for event in events],
    }
    results = []
    if config.sequence:
        first = {}
        for event in events:
            first.setdefault(event.name, event.sequence)
        positions = [first.get(name) for name in config.sequence]
        passed = all(value is not None for value in positions) and positions == sorted(positions)
        status = "NA" if not complete else "PASS" if passed else "FAIL"
        results.append(MetricResult(metric="tool_order", status=status, required="tool_order" in required,
            reason="TOOL_TRAJECTORY_INCOMPLETE" if not complete else "TOOL_FIRST_OCCURRENCE_ORDER_MATCH" if passed else "TOOL_FIRST_OCCURRENCE_ORDER_MISMATCH",
            evidence=evidence | {"dependencies": RULE_DEPENDENCIES["tool_order"], "expected_sequence": config.sequence,
                                 "first_positions": first, "semantics": "FIRST_OCCURRENCE_ORDER_ALL_REQUIRED"}))
    if config.max_calls:
        counts = {name: sum(event.name == name for event in events) for name in config.max_calls}
        exceeded = [name for name, limit in config.max_calls.items() if counts[name] > limit]
        status = "NA" if not complete else "FAIL" if exceeded else "PASS"
        results.append(MetricResult(metric="tool_call_count", status=status, required="tool_call_count" in required,
            reason="TOOL_TRAJECTORY_INCOMPLETE" if not complete else "TOOL_CALL_LIMIT_EXCEEDED" if exceeded else "TOOL_CALL_LIMITS_MET",
            evidence=evidence | {"dependencies": RULE_DEPENDENCIES["tool_call_count"], "limits": config.max_calls,
                                 "actual_counts": counts, "exceeded": exceeded}))
    return results


def combine_metrics(case_snapshot: Mapping[str, Any], rule_profile: RuleProfile | Mapping[str, Any], rules: list[MetricResult], judge: JudgeResult) -> CaseAssessment:
    profile, required = _profile(rule_profile), required_metrics(case_snapshot)
    if len({metric.metric for metric in rules}) != len(rules):
        raise ValueError("DUPLICATE_METRIC")
    results = [metric.model_copy(update={"required": metric.metric in required}) for metric in rules]
    by_name = {metric.metric: metric for metric in results}
    facts = by_name.get("execution_completed")
    facts = facts.evidence if facts is not None else {}
    for name in QUALITY_METRICS:
        dependencies = ["question", "answer", "expected_answer"]
        evidence = {"rule_version": profile.version, "threshold": profile.quality_thresholds.get(name, 4), "dependencies": dependencies.copy()}
        dependency_reason = None
        if not case_snapshot.get("question") or not case_snapshot.get("expected_answer") or not facts.get("answer_available"):
            dependency_reason = "QUALITY_INPUT_UNAVAILABLE"
        if name == "faithfulness":
            dependencies.extend(["actual_model_context", "complete_context"])
            evidence.update({"dependencies": dependencies, "context_scope": facts.get("context_scope", "UNAVAILABLE"), "context_completeness": facts.get("context_completeness", "UNAVAILABLE"), "context_count": facts.get("context_count", 0)})
            if facts.get("context_scope") == "CITATION_SUBSET":
                dependency_reason = "CITATION_SUPPORT_ONLY_NOT_FULL_CONTEXT"
                evidence["citation_support_only"] = True
            elif facts.get("context_scope") == "NOT_USED":
                dependency_reason = "CONTEXT_NOT_USED"
            elif facts.get("context_scope") != "ACTUAL_MODEL_CONTEXT" or facts.get("context_completeness") != "COMPLETE" or not facts.get("context_count"):
                dependency_reason = "COMPLETE_ACTUAL_MODEL_CONTEXT_UNAVAILABLE"
        if dependency_reason:
            result = MetricResult(metric=name, status="NA", required=name in required, reason=dependency_reason, evidence=evidence)
        elif judge.status == "ERROR":
            result = MetricResult(metric=name, status="ERROR", required=name in required, reason=judge.error_code or "JUDGE_ERROR", evidence=evidence)
        elif judge.status != "SUCCEEDED" or name not in judge.scores:
            result = MetricResult(metric=name, status="NA", required=name in required, reason="JUDGE_METRIC_UNAVAILABLE", evidence=evidence)
        else:
            score = judge.scores[name]
            result = MetricResult(metric=name, status="PASS" if score.score >= evidence["threshold"] else "FAIL", score=score.score, required=name in required, reason=score.reason, evidence=evidence)
        if name in by_name:
            raise ValueError("QUALITY_METRIC_MUST_COME_FROM_JUDGE")
        results.append(result)
        by_name[name] = result
    for missing in sorted(required - by_name.keys()):
        result = MetricResult(metric=missing, status="NA", required=True, reason="REQUIRED_METRIC_MISSING", evidence={"rule_version": profile.version})
        results.append(result)
        by_name[missing] = result
    required_results = [by_name[name] for name in required]
    conclusion = "FAIL" if any(metric.status == "FAIL" for metric in required_results) else "PASS" if all(metric.status == "PASS" for metric in required_results) else "UNDETERMINED"
    return CaseAssessment(metrics=results, conclusion=conclusion, judge_status=judge.status)
