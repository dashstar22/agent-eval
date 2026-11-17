import pytest

from agenteval.evaluation_models import JudgeResult, JudgedScore, MetricResult
from agenteval.models import Context, ExecutionResult, ToolEvent, now
from agenteval.rules import combine_metrics, evaluate_rules, tool_statistics
from agenteval.schemas import DEFAULT_REQUIRED, RuleProfile


def case(**changes):
    return {"case_key": "c1", "question": "卡纸如何处理？", "expected_answer": "先断电，按手册安全清理。", "expected_tools": None,
            "tool_match_mode": "contains", "required_metrics": DEFAULT_REQUIRED.copy()} | changes


def execution(**changes):
    return ExecutionResult.model_validate({"agent_status": "SUCCEEDED", "answer": "先断电，按手册安全清理。", "source_type": "MOCK",
        "started_at": now(), "ended_at": now(), "duration_ms": 10, "tools": [], "tools_completeness": "COMPLETE"} | changes)


def event(name="searchKnowledge", status="HIT", source="CHAT_TOOL", **changes):
    return ToolEvent.model_validate({"event_id": "e1", "sequence": 1, "source": source, "name": name, "status": status} | changes)


def metrics(c=None, e=None, profile=None):
    return {metric.metric: metric for metric in evaluate_rules(c or case(), e or execution(), profile or RuleProfile())}


def judge(score=4, **extra):
    return JudgeResult(status="SUCCEEDED", scores={name: JudgedScore(score=score, reason="符合参考行为") for name in ("correctness", "relevance", "task_completion")} | extra)


def test_six_rules_with_frozen_versions_and_time_threshold():
    result = metrics(e=execution(answer="  ", duration_ms=11), profile=RuleProfile(version="r2", response_time_ms=10))
    assert len(result) == 6
    assert result["execution_completed"].status == "PASS"
    assert result["answer_nonempty"].status == result["response_time"].status == "FAIL"
    assert all(metric.evidence["rule_version"] == "r2" for metric in result.values())
    assert result["response_time"].evidence == {"rule_version": "r2", "dependencies": ["agent_status", "duration_ms", "response_time_ms"], "actual_ms": 11, "threshold_ms": 10}


@pytest.mark.parametrize("status,expected", [("TIMEOUT", "FAIL"), ("FAILED", "FAIL"), ("PROTOCOL_ERROR", "FAIL"), ("RESULT_UNKNOWN", "NA")])
def test_execution_state_is_rule_fact_independent_of_judge(status, expected):
    result = metrics(e=execution(agent_status=status, answer=None))
    assert result["execution_completed"].status == expected
    assert result["response_time"].status == "NA"


def test_unknown_result_without_answer_is_missing_evidence_not_confirmed_empty_answer():
    c, e = case(), execution(agent_status="RESULT_UNKNOWN", answer=None)
    rules = evaluate_rules(c, e, RuleProfile())
    by_name = {metric.metric: metric for metric in rules}
    assert by_name["execution_completed"].status == "NA"
    assert by_name["answer_nonempty"].status == "NA"
    assert by_name["answer_nonempty"].reason == "ANSWER_UNAVAILABLE_RESULT_UNKNOWN"
    assert by_name["answer_nonempty"].evidence["non_whitespace_length"] is None
    result = combine_metrics(c, RuleProfile(), rules, JudgeResult(status="ERROR", error_code="JUDGE_UNAVAILABLE"))
    assert result.conclusion == "UNDETERMINED"
    assert metrics(e=execution(answer=""))["answer_nonempty"].status == "FAIL"
    assert metrics(e=execution(agent_status="TIMEOUT", answer=None))["answer_nonempty"].status == "FAIL"


def test_contains_and_exact_have_distinct_extra_tool_behavior():
    result = execution(tools=[event(), event("getOrder", event_id="e2", sequence=2)])
    assert metrics(case(expected_tools=["searchKnowledge"]), result)["tool_names"].status == "PASS"
    exact = metrics(case(expected_tools=["searchKnowledge"], tool_match_mode="exact"), result)
    assert exact["expected_tools"].status == "PASS"
    assert exact["tool_names"].status == "FAIL"


def test_null_expectation_empty_exact_and_unknown_trajectory_differ():
    assert metrics()["expected_tools"].reason == "EXPECTED_TOOLS_NOT_CONFIGURED"
    assert metrics(case(expected_tools=[], tool_match_mode="exact"))["tool_names"].status == "PASS"
    assert metrics(case(expected_tools=[], tool_match_mode="exact"), execution(tools=[event()]))["tool_names"].status == "FAIL"
    unknown = metrics(case(expected_tools=["searchKnowledge"]), execution(tools=None, tools_completeness="UNAVAILABLE"))
    empty = metrics(case(expected_tools=["searchKnowledge"]), execution())
    assert unknown["expected_tools"].status == "NA"
    assert empty["expected_tools"].status == "FAIL"


def test_partial_trajectory_proves_presence_or_counterexample_but_not_absence():
    result = execution(tools=[event()], tools_completeness="PARTIAL")
    assert metrics(case(expected_tools=["searchKnowledge"]), result)["tool_names"].status == "PASS"
    assert metrics(case(expected_tools=["getOrder"]), result)["expected_tools"].status == "NA"
    assert metrics(case(expected_tools=["searchKnowledge"], tool_match_mode="exact"), result)["tool_names"].status == "NA"
    assert metrics(case(expected_tools=[], tool_match_mode="exact"), result)["tool_names"].status == "FAIL"


def test_sources_and_event_kind_are_filtered_and_unknown_source_not_proven_absence():
    coordinator = execution(tools=[event(source="SERVER_COORDINATOR")])
    assert metrics(case(expected_tools=["searchKnowledge"]), coordinator)["expected_tools"].status == "FAIL"
    profile = RuleProfile(tool_sources=["CHAT_TOOL", "SERVER_COORDINATOR"])
    assert metrics(case(expected_tools=["searchKnowledge"]), coordinator, profile)["expected_tools"].status == "PASS"
    unknown = execution(tools=[event(source="UNKNOWN")])
    assert metrics(case(expected_tools=["searchKnowledge"]), unknown)["expected_tools"].status == "NA"
    non_tool = execution(tools=[event(kind="MODEL")])
    assert tool_statistics(non_tool)["event_count"] == 0
    assert metrics(case(expected_tools=["searchKnowledge"]), non_tool)["expected_tools"].status == "FAIL"


@pytest.mark.parametrize("status,category,rule_status", [
    ("ORDER_FOUND", "normal_completion", "PASS"), ("HIT", "normal_completion", "PASS"),
    ("NO_EVIDENCE", "normal_completion", "PASS"), ("PAGE_CONFIRMATION_REQUIRED", "business_refusal", "PASS"),
    ("AUTHORIZATION_DENIED", "business_refusal", "PASS"), ("TOOL_TIMEOUT", "technical_failure", "FAIL"),
    ("RETRIEVAL_UNAVAILABLE", "technical_failure", "FAIL"), ("200", "unknown", "NA"), ("NEW_STATE", "unknown", "NA"),
])
def test_versioned_tool_outcomes_do_not_confuse_business_denial_with_failure(status, category, rule_status):
    result = execution(tools=[event(status=status)])
    stats = tool_statistics(result)
    assert stats[category] == 1
    assert metrics(e=result)["tool_success"].status == rule_status
    if category in {"business_refusal", "unknown"}:
        assert stats["success_rate"] is None and stats["denominator"] == 0


def test_success_rate_denominator_excludes_unknown_and_business_refusal():
    result = execution(tools=[event(status=status, event_id=f"e{i}", sequence=i) for i, status in enumerate(["HIT", "TOOL_TIMEOUT", "PAGE_CONFIRMATION_REQUIRED", "UNKNOWN"], 1)])
    stats = tool_statistics(result)
    assert stats["success_rate"] == 0.5 and stats["denominator"] == 2
    assert stats["business_refusal"] == stats["unknown"] == 1
    assert tool_statistics(execution())["success_rate"] is None


def test_failed_tool_remains_known_failure_when_trajectory_partial():
    assert metrics(e=execution(tools=[event(status="TOOL_TIMEOUT")], tools_completeness="PARTIAL"))["tool_success"].status == "FAIL"


def test_not_executed_and_all_optional_configuration_are_not_vacuous_pass():
    rules = evaluate_rules(case(), None, RuleProfile())
    result = combine_metrics(case(), RuleProfile(), rules, judge())
    assert result.conclusion == "UNDETERMINED" and all(metric.status == "NA" for metric in result.metrics)
    with pytest.raises(ValueError, match="REQUIRED_METRICS"):
        evaluate_rules(case(required_metrics=[]), execution(), RuleProfile())


def test_default_thresholds_and_default_required_metrics():
    c, e = case(), execution()
    result = combine_metrics(c, RuleProfile(), evaluate_rules(c, e, RuleProfile()), judge())
    assert result.conclusion == "PASS"
    assert {metric.metric for metric in result.metrics if metric.required} == set(DEFAULT_REQUIRED)
    assert next(metric for metric in result.metrics if metric.metric == "faithfulness").status == "NA"
    result = combine_metrics(c, RuleProfile(), evaluate_rules(c, e, RuleProfile()), judge(3))
    assert result.conclusion == "FAIL"


def test_known_required_failure_wins_over_independent_judge_error():
    c, e = case(), execution(agent_status="TIMEOUT", answer=None)
    failure = JudgeResult(status="ERROR", error_code="JUDGE_UNAVAILABLE")
    result = combine_metrics(c, RuleProfile(), evaluate_rules(c, e, RuleProfile()), failure)
    assert result.conclusion == "FAIL" and result.judge_status == "ERROR"
    e = execution()
    result = combine_metrics(c, RuleProfile(), evaluate_rules(c, e, RuleProfile()), failure)
    assert result.conclusion == "UNDETERMINED"
    assert {metric.status for metric in result.metrics if metric.metric in ("correctness", "relevance", "task_completion")} == {"ERROR"}
    assert e.agent_status == "SUCCEEDED"


@pytest.mark.parametrize("scope,complete,contexts,faithfulness_status", [
    ("ACTUAL_MODEL_CONTEXT", "COMPLETE", [Context(text="按手册断电")], "PASS"),
    ("ACTUAL_MODEL_CONTEXT", "PARTIAL", [Context(text="部分")], "NA"),
    ("ACTUAL_MODEL_CONTEXT", "COMPLETE", [], "NA"),
    ("CITATION_SUBSET", "COMPLETE", [Context(text="引用")], "NA"),
    ("NOT_USED", "COMPLETE", [], "NA"),
])
def test_faithfulness_requires_complete_actual_context_and_required_missing_never_passes(scope, complete, contexts, faithfulness_status):
    c = case(required_metrics=DEFAULT_REQUIRED + ["faithfulness"])
    e = execution(context_scope=scope, context_completeness=complete, contexts=contexts)
    scores = judge(faithfulness=JudgedScore(score=5, reason="支持"))
    result = combine_metrics(c, RuleProfile(), evaluate_rules(c, e, RuleProfile()), scores)
    faithfulness = next(metric for metric in result.metrics if metric.metric == "faithfulness")
    assert faithfulness.status == faithfulness_status
    assert result.conclusion == ("PASS" if faithfulness_status == "PASS" else "UNDETERMINED")
    if scope == "CITATION_SUBSET":
        assert faithfulness.evidence["citation_support_only"] is True and faithfulness.score is None


def test_missing_required_metric_and_missing_judge_score_are_undetermined():
    c = case(required_metrics=["tool_names"])
    result = combine_metrics(c, RuleProfile(), [], JudgeResult(status="NA"))
    assert result.conclusion == "UNDETERMINED"
    missing = next(metric for metric in result.metrics if metric.metric == "tool_names")
    assert missing.required and missing.reason == "REQUIRED_METRIC_MISSING"
    c, e = case(), execution()
    result = combine_metrics(c, RuleProfile(), evaluate_rules(c, e, RuleProfile()), JudgeResult(status="SUCCEEDED", scores={}))
    assert result.conclusion == "UNDETERMINED"


def test_duplicate_metrics_are_rejected():
    item = MetricResult(metric="answer_nonempty", status="PASS", reason="observed")
    with pytest.raises(ValueError, match="DUPLICATE_METRIC"):
        combine_metrics(case(), RuleProfile(), [item, item], judge())
