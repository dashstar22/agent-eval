import pytest
from pydantic import ValidationError

from agenteval.rules import evaluate_rules
from agenteval.schemas import CaseCreate, RuleProfile
from test_rules import case, execution, event


def configured(**changes):
    return case(tool_assertions={"sequence": ["lookup", "write"], "max_calls": {"write": 1}}, **changes)


def result(c, e):
    return {item.metric: item for item in evaluate_rules(c, e, RuleProfile())}


def test_old_cases_keep_six_rules_and_new_assertions_cite_events():
    assert len(result(case(), execution())) == 6
    events = [event("lookup", event_id="a"), event("write", event_id="b", sequence=2)]
    data = result(configured(), execution(tools=events))
    assert data["tool_order"].status == data["tool_call_count"].status == "PASS"
    assert data["tool_order"].evidence["events"][1] == {"event_id": "b", "sequence": 2, "name": "write"}
    assert data["tool_call_count"].evidence["actual_counts"] == {"write": 1}


def test_first_write_before_lookup_fails_even_if_later_valid_subsequence():
    events = [event("write", event_id="a"), event("lookup", event_id="b", sequence=2), event("write", event_id="c", sequence=3)]
    data = result(configured(), execution(tools=events))
    assert data["tool_order"].status == "FAIL"
    assert data["tool_call_count"].status == "FAIL"
    assert data["tool_call_count"].evidence["exceeded"] == ["write"]


@pytest.mark.parametrize("completeness", ["PARTIAL", "UNAVAILABLE"])
def test_incomplete_trace_cannot_prove_order_or_count(completeness):
    data = result(configured(), execution(tools=[], tools_completeness=completeness))
    assert data["tool_order"].status == data["tool_call_count"].status == "NA"


def test_unknown_source_and_unexecuted_are_not_empty_trace():
    data = result(configured(), execution(tools=[event("write", source="UNKNOWN")]))
    assert data["tool_order"].status == data["tool_call_count"].status == "NA"
    data = result(configured(), None)
    assert data["tool_order"].status == "NA"


def test_source_scope_and_zero_limit():
    c = case(tool_assertions={"max_calls": {"write": 0}})
    data = result(c, execution(tools=[event("write", source="SERVER_COORDINATOR")]))
    assert data["tool_call_count"].status == "PASS"
    data = result(c, execution(tools=[event("write")]))
    assert data["tool_call_count"].status == "FAIL"


@pytest.mark.parametrize("assertions", [
    {"sequence": []}, {"sequence": ["lookup", "lookup"]}, {"sequence": [" "]},
    {"max_calls": {"write": -1}}, {"max_calls": {"write": True}}, {"max_calls": {"write": 1.5}},
])
def test_invalid_assertion_config_rejected(assertions):
    with pytest.raises(ValidationError):
        CaseCreate(case_key="A", question="问题", expected_answer="参考行为", tool_assertions=assertions)


@pytest.mark.parametrize("metric", ["tool_order", "tool_call_count"])
def test_required_assertion_requires_config(metric):
    with pytest.raises(ValidationError):
        CaseCreate(case_key="A", question="问题", expected_answer="参考行为", required_metrics=[metric])
