import pytest
from pydantic import ValidationError

from agenteval.evaluation_models import CaseAssessment, JudgeAttemptRecord, JudgeResult, MetricResult, SummaryCase
from agenteval.models import ExecutionResult, ObservedVersion, ToolEvent, Usage, UsageAttempt, now
from agenteval.schemas import RuleProfile
from agenteval.statistics import Price, summarize


def execution(**changes):
    return ExecutionResult.model_validate({"agent_status": "SUCCEEDED", "answer": "参考行为", "started_at": now(), "ended_at": now(),
        "duration_ms": 10, "source_type": "MOCK", "tools": [], "tools_completeness": "COMPLETE"} | changes)


def assessment(conclusion="PASS", score=4):
    return CaseAssessment(conclusion=conclusion, metrics=[MetricResult(metric="correctness", status="PASS" if score >= 4 else "FAIL", score=score, required=True, reason="符合参考行为")])


def full_usage(input_tokens=10, output_tokens=2, **changes):
    return Usage.model_validate({"status": "COMPLETE", "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens} | changes)


def price(model="model-a", currency="CNY"):
    return Price(model=model, currency=currency, unit_tokens=1000, input_per_unit=2.0, output_per_unit=8.0,
        effective_source="https://example.test/price-fixture", effective_from="2026-11-16")


def test_fixed_thirty_case_denominator_and_twenty_five_five_example():
    keys = [f"c{i}" for i in range(30)]
    items = [SummaryCase(case_key=key, assessment=assessment("PASS" if i < 20 else "FAIL")) for i, key in enumerate(keys[:25])]
    result = summarize(keys, items)
    assert (result["N"], result["P"], result["F"], result["U"]) == (30, 20, 5, 5)
    assert result["effective_pass_rate"] == 0.8
    assert result["determinate_coverage"] == 25 / 30
    assert result["metric_averages"]["correctness"]["valid_count"] == 25
    assert result["metric_averages"]["correctness"]["total_count"] == 30


def test_all_zero_denominators_and_unexecuted_never_become_zero_rates():
    result = summarize([], [])
    assert result["effective_pass_rate"] is None and result["determinate_coverage"] is None
    assert result["tools"]["success_rate"] is None
    assert result["agent"]["timeout_rate"] is None
    assert result["metric_averages"]["correctness"]["mean"] is None
    pending = summarize(["a", "b"], [], run_status="RUNNING")
    assert pending["U"] == 2 and pending["N"] == 2 and pending["provisional"]
    interrupted = summarize(["a", "b"], [], run_status="INTERRUPTED")
    assert interrupted["interrupted"] and interrupted["U"] == 2


@pytest.mark.parametrize("keys,items,error", [
    (["a", "a"], [], "FROZEN_CASE_KEYS"),
    (["a"], [SummaryCase(case_key="a"), SummaryCase(case_key="a")], "SUMMARY_CASE_KEYS"),
    (["a"], [SummaryCase(case_key="outside")], "SUMMARY_CASE_NOT"),
])
def test_mismatched_or_duplicate_cases_are_rejected(keys, items, error):
    with pytest.raises(ValueError, match=error):
        summarize(keys, items)


def test_success_latency_excludes_timeout_and_failed_and_timeout_has_explicit_denominator():
    result = summarize(["a", "b", "c", "d"], [
        SummaryCase(case_key="a", execution=execution(duration_ms=20)),
        SummaryCase(case_key="b", execution=execution(duration_ms=1000, agent_status="TIMEOUT", answer=None)),
        SummaryCase(case_key="c", execution=execution(duration_ms=500, agent_status="FAILED", answer=None)),
    ])
    assert result["agent"]["success_duration_mean_ms"] == 20
    assert result["agent"]["success_duration_valid_count"] == 1
    assert result["agent"]["timeout_count"] == 1
    assert result["agent"]["timeout_rate"] == 1 / 3 and result["agent"]["timeout_denominator"] == 3
    assert result["agent"]["status_counts"]["NOT_EXECUTED"] == 1


def test_valid_metric_is_kept_when_other_required_metric_is_missing():
    item = SummaryCase(case_key="a", assessment=assessment("UNDETERMINED", score=5))
    result = summarize(["a", "b"], [item])
    assert result["U"] == 2
    assert result["metric_averages"]["correctness"] == {"mean": 5.0, "valid_count": 1, "total_count": 2}


def test_tool_denominators_sources_unknown_and_trajectory_scope_are_visible():
    events = [ToolEvent(event_id=f"e{i}", sequence=i, source=source, name="search", status=status)
              for i, (source, status) in enumerate([("CHAT_TOOL", "HIT"), ("CHAT_TOOL", "TOOL_TIMEOUT"),
                  ("CHAT_TOOL", "PAGE_CONFIRMATION_REQUIRED"), ("CHAT_TOOL", "UNKNOWN"), ("SERVER_COORDINATOR", "HIT"), ("UNKNOWN", "HIT")], 1)]
    result = summarize(["a", "b"], [SummaryCase(case_key="a", execution=execution(tools=events))])
    assert result["tools"]["success_rate"] == 0.5
    assert result["tools"]["denominator"] == 2
    assert result["tools"]["business_refusal"] == result["tools"]["unknown"] == result["tools"]["unknown_source"] == 1
    assert result["tools"]["trajectory_counts"] == {"PARTIAL": 1, "NOT_EXECUTED": 1}
    inclusive = summarize(["a"], [SummaryCase(case_key="a", execution=execution(tools=events))], rule_profile=RuleProfile(tool_sources=["CHAT_TOOL", "SERVER_COORDINATOR"]))
    assert inclusive["tools"]["success_rate"] == 2 / 3


def test_agent_judge_usage_are_separate_and_partial_is_not_complete_or_zero_cost():
    agent = execution(usage=Usage(status="PARTIAL", input_tokens=30))
    judge = JudgeResult(status="ERROR", attempts=[JudgeAttemptRecord(attempt=1, status="ERROR", usage=full_usage(5, 1))])
    result = summarize(["a", "b"], [SummaryCase(case_key="a", execution=agent, judge=judge), SummaryCase(case_key="b", execution=execution())])
    a, j = result["usage"]["Agent"], result["usage"]["Judge"]
    assert a["input_tokens"] == 30 and a["output_tokens"] is None and a["total_tokens"] is None
    assert a["known_input_tokens_records"] == 1 and a["applicable_records"] == 2
    assert a["known_part_only"] and a["availability"] == "PARTIAL"
    assert j["input_tokens"] == 5 and j["total_tokens"] == 6
    assert a["cost"] is None and j["cost"] is None and not a["known_cost_by_currency"]


def test_usage_attempts_are_not_double_counted_and_missing_attempt_counters_keep_partial_scope():
    usage = Usage(status="PARTIAL", input_tokens=10, output_tokens=2, total_tokens=12, attempts=[
        UsageAttempt(attempt=1, availability="COMPLETE", model_id="model-a", input_tokens=10, output_tokens=2, total_tokens=12),
        UsageAttempt(attempt=2, availability="UNAVAILABLE"),
    ])
    result = summarize(["a"], [SummaryCase(case_key="a", execution=execution(usage=usage))], prices=[price()])
    a = result["usage"]["Agent"]
    assert a["total_tokens"] == 12 and a["applicable_records"] == 2
    assert a["availability"] == "PARTIAL" and a["cost"] is None
    assert a["known_cost_by_currency"]["CNY"] == pytest.approx(0.036)
    assert a["cost_known_part_only"]


def test_partial_parent_usage_never_claims_complete_only_because_known_attempts_are_complete():
    usage = Usage(status="PARTIAL", input_tokens=10, output_tokens=2, total_tokens=12, attempts=[
        UsageAttempt(attempt=1, availability="COMPLETE", model_id="model-a", input_tokens=10, output_tokens=2, total_tokens=12)])
    a = summarize(["a"], [SummaryCase(case_key="a", execution=execution(usage=usage))], prices=[price()])["usage"]["Agent"]
    assert a["total_tokens"] == 12 and a["known_part_only"] and a["cost"] is None


def test_prerequisite_nested_histories_are_deduplicated_and_replay_adds_no_model_tokens():
    first = execution(usage=full_usage(10, 2))
    second = execution(usage=full_usage(20, 3), prerequisites=[first.model_dump()])
    final = execution(usage=full_usage(30, 4), prerequisites=[first.model_dump(), second.model_dump()])
    replay = execution(replayed=True, usage=full_usage(1000, 100))
    result = summarize(["a", "b"], [SummaryCase(case_key="a", execution=final), SummaryCase(case_key="b", execution=replay)])
    a = result["usage"]["Agent"]
    assert a["input_tokens"] == 60 and a["output_tokens"] == 9 and a["total_tokens"] == 69
    assert a["applicable_records"] == 3 and a["not_requested_records"] == 1


def test_valid_prices_compute_only_actual_model_and_retain_source_metadata():
    e = execution(usage=full_usage(), observed_version=ObservedVersion(model_id="model-a"))
    a = summarize(["a"], [SummaryCase(case_key="a", execution=e)], prices=[price()])["usage"]["Agent"]
    assert a["cost"] == pytest.approx(0.036) and a["cost_currency"] == "CNY"
    assert a["price_configuration"] == [price().model_dump()]
    missing_model = summarize(["a"], [SummaryCase(case_key="a", execution=execution(usage=full_usage()))], prices=[price()])["usage"]["Agent"]
    assert missing_model["cost"] is None and not missing_model["known_cost_by_currency"]


def test_multiple_price_currencies_are_not_added_together():
    items = [SummaryCase(case_key=model, execution=execution(usage=full_usage(), observed_version=ObservedVersion(model_id=model))) for model in ("model-a", "model-b")]
    a = summarize(["model-a", "model-b"], items, prices=[price(), price("model-b", "USD")])["usage"]["Agent"]
    assert a["cost"] is None and set(a["known_cost_by_currency"]) == {"CNY", "USD"}
    assert a["cost_reason"] == "MULTIPLE_CURRENCIES"
    with pytest.raises(ValueError, match="PRICE_MODEL_MUST_BE_UNIQUE"):
        summarize(["a"], [], prices=[price(), price()])


@pytest.mark.parametrize("changes", [{"input_per_unit": -1.0}, {"unit_tokens": 0}, {"output_per_unit": float("nan")}, {"effective_source": ""}, {"currency": " "}])
def test_invalid_price_configuration_rejected(changes):
    with pytest.raises(ValidationError):
        Price.model_validate(price().model_dump() | changes)


@pytest.mark.parametrize("agent_source,agent_verified,judge_source,expected", [
    ("MOCK", False, "REAL_EXTERNAL", False),
    ("REAL_EXTERNAL", True, "PROTOCOL_DOUBLE", False),
    ("REAL_EXTERNAL", True, "UNKNOWN", False),
    ("REAL_EXTERNAL", True, "REAL_EXTERNAL", True),
])
def test_quality_evidence_requires_real_agent_and_real_judge_provenance(agent_source, agent_verified, judge_source, expected):
    e = execution(source_type=agent_source, model_execution_verified=agent_verified)
    j = JudgeResult(status="SUCCEEDED", attempts=[JudgeAttemptRecord(attempt=1, status="SUCCEEDED", request={"data": {"source_type": judge_source}})])
    scope = summarize(["a"], [SummaryCase(case_key="a", execution=e, judge=j)])["source_scope"]
    assert scope["real_quality_evidence"] is expected
    assert scope["judge_attempt_source_counts"][judge_source] == 1


def test_judge_failure_does_not_claim_real_quality_even_with_real_external_attempt():
    e = execution(source_type="REAL_EXTERNAL", model_execution_verified=True)
    j = JudgeResult(status="ERROR", attempts=[JudgeAttemptRecord(attempt=1, status="ERROR", request={"data": {"source_type": "REAL_EXTERNAL"}})])
    scope = summarize(["a"], [SummaryCase(case_key="a", execution=e, judge=j)])["source_scope"]
    assert scope["verified_real_agent_execution"] and not scope["real_quality_evidence"]


@pytest.mark.parametrize("score", [float("nan"), float("inf"), 0, 6, 4.5])
def test_corrupt_quality_scores_are_rejected_instead_of_polluting_means(score):
    item = SummaryCase(case_key="a", assessment=assessment(score=score))
    with pytest.raises(ValueError, match="INVALID_QUALITY_SCORE"):
        summarize(["a"], [item])
