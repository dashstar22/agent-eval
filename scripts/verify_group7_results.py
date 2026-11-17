"""Independently audit saved real A/B evidence, using only the standard library.

Reads one JSON artifact and writes a separate audit. No HTTP client, database,
product evaluator, model invocation, source mutation, or automatic rerun exists.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUALITY = {"correctness", "relevance", "task_completion", "faithfulness"}
RULES = {"execution_completed", "answer_nonempty", "expected_tools", "tool_success", "response_time", "tool_names"}
COUNTERS = ("input_tokens", "output_tokens", "total_tokens")
TERMINAL = {"COMPLETED", "FAILED", "INTERRUPTED"}
TECHNICAL_BUSINESS_STATUSES = {"MODEL_UNAVAILABLE", "TURN_TIMEOUT", "MODEL_TIMEOUT", "ASSISTANT_BUSY", "DATABASE_UNAVAILABLE", "WRITE_RESULT_UNKNOWN",
    "ASSISTANT_NOT_CONNECTED", "RETRIEVAL_UNAVAILABLE", "MODEL_RESPONSE_REJECTED", "TOOL_TIMEOUT", "ASSISTANT_CANCELLED", "CHECKPOINT_UNAVAILABLE",
    "MODEL_CALL_LIMIT", "TOOL_CALL_LIMIT", "AI_DISABLED", "RESULT_UNKNOWN"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def unwrap(value):
    return value.get("data") if isinstance(value, dict) and "data" in value and "completeness" in value else value


def ratio(n, d):
    return n / d if d else None


def near(left, right):
    if left is None or right is None:
        return left is right
    return isinstance(left, (int, float)) and isinstance(right, (int, float)) and math.isclose(left, right, abs_tol=1e-10)


def status_of(checks):
    return "FAIL" if any(row["status"] == "FAIL" for row in checks) else "UNVERIFIED" if any(row["status"] == "UNVERIFIED" for row in checks) else "PASS"


def report_snapshot_matches(report_snapshot, original):
    if report_snapshot == original:
        return True
    # The report sanitizer redacts this reference field name even though the
    # /runs endpoint deliberately preserves references for snapshot hashing.
    # Permit exactly this known projection, never arbitrary text differences.
    projected = json.loads(canonical(original))
    refs = projected.get("agent", {}).get("credential_refs", {})
    if "password_ref" in refs:
        refs["password_ref"] = "[REDACTED]"
    return report_snapshot == projected


class Audit:
    def __init__(self):
        self.checks = []
        self.case_audits = []
        self.run_audits = {}
        self.successful_pair_model_changes = []

    def check(self, name, condition, *, missing=False, **evidence):
        coverage = any(marker in name for marker in (":successful_agent_real_model_evidence", ":supported_quality_has_real_judge", ":actual_model_change_verified", ":model_change_backed_by_actual_metadata"))
        category = "REAL_COVERAGE" if coverage else "WORKFLOW_COMPLETION" if name.endswith(":batch_completed") else "STRUCTURAL_INTEGRITY"
        self.checks.append({"name": name, "category": category, "status": "PASS" if condition else "UNVERIFIED" if missing else "FAIL", **evidence})

    def usage(self, name, usage):
        if not isinstance(usage, dict):
            self.check(name + ":usage_present", False, missing=True)
            return
        availability = usage.get("availability", usage.get("status"))
        values = [usage.get(key) for key in COUNTERS]
        self.check(name + ":usage_availability", availability in {"COMPLETE", "PARTIAL", "UNAVAILABLE", "NOT_REQUESTED"})
        self.check(name + ":nonnegative_real_counters", all(value is None or type(value) is int and value >= 0 for value in values))
        self.check(name + ":complete_requires_all_counters", availability != "COMPLETE" or all(value is not None for value in values))
        self.check(name + ":unavailable_has_no_fabricated_counters", availability not in {"UNAVAILABLE", "NOT_REQUESTED"} or all(value is None for value in values))
        self.check(name + ":unknown_cost_is_null", usage.get("cost") is None)
        for attempt in usage.get("attempts", []):
            self.usage(name + ":attempt:" + str(attempt.get("attempt")), attempt)

    def case(self, variant, detail, run, frozen, score_id):
        case, scoring = detail.get("case", {}), detail.get("scoring", {})
        key, identity = case.get("case_key"), case.get("id")
        prefix = variant + ":" + str(key)
        start = len(self.checks)
        self.check(prefix + ":same_run_case_score", bool(identity) and case.get("run_id") == run["id"] and
                   scoring.get("run_id") == run["id"] and scoring.get("case_run_id") == identity and
                   scoring.get("score_set_id") == score_id and detail.get("score_set", {}).get("run_id") == run["id"] and
                   detail.get("score_set", {}).get("id") == score_id)
        self.check(prefix + ":same_frozen_case", key in frozen and case.get("case_snapshot") == frozen.get(key))
        metrics = scoring.get("metrics", [])
        evaluation = scoring.get("evaluation") or {}
        conclusion = evaluation.get("conclusion", "UNDETERMINED")
        self.check(prefix + ":conclusion_valid", conclusion in {"PASS", "FAIL", "UNDETERMINED"})
        execution = (case.get("raw_agent") or {}).get("normalized_execution")
        explicit_error = (case.get("error") or {}).get("code") or run.get("error")
        if isinstance(execution, dict) and execution.get("agent_status") != "SUCCEEDED" and execution.get("business_status") in TECHNICAL_BUSINESS_STATUSES:
            explicit_error = explicit_error or execution["business_status"]
        self.check(prefix + ":execution_or_explicit_failure", isinstance(execution, dict) or bool(explicit_error), missing=not explicit_error)
        result = {"variant": variant, "case_key": key, "case_run_id": identity, "run_id": run["id"], "score_set_id": score_id,
                  "agent_status": case.get("agent_status"), "conclusion": conclusion,
                  "explicit_error": explicit_error, "evaluation_issues": detail.get("evaluation_issues", []),
                  "real_agent_verified": False, "real_judge_success": False}
        if isinstance(execution, dict):
            self.check(prefix + ":display_matches_saved_execution", case.get("answer") == execution.get("answer") and
                       case.get("agent_status") == execution.get("agent_status") and case.get("usage") == execution.get("usage") and
                       case.get("timing", {}).get("duration_ms") == execution.get("duration_ms"))
            self.usage(prefix + ":Agent", execution.get("usage"))
            provider = [attempt for attempt in execution.get("usage", {}).get("attempts", [])
                        if attempt.get("status") == "SUCCEEDED" and attempt.get("source") == "MODEL_RESPONSE_METADATA" and attempt.get("model_id")]
            observation = execution.get("raw_agent", {}).get("observation") or {}
            version = execution.get("observed_version") or {}
            model_id = version.get("model_id")
            result["real_agent_verified"] = (execution.get("source_type") == "REAL_EXTERNAL" and execution.get("model_execution_verified") is True and
                observation.get("sourceType") == "HTTP_AGENT" and observation.get("modelMode") == "REMOTE_API_UNVERIFIED" and
                any(model_id in {attempt.get("model_id"), attempt.get("requested_model_id")} for attempt in provider) and bool(model_id))
            self.check(prefix + ":no_simulated_agent", execution.get("source_type") == "REAL_EXTERNAL")
            if execution.get("agent_status") == "SUCCEEDED":
                self.check(prefix + ":successful_agent_real_model_evidence", result["real_agent_verified"], missing=True)
            else:
                self.check(prefix + ":failed_execution_reason_retained", bool(execution.get("error_code") or explicit_error))
            result.update({"execution_id": execution.get("execution_id"), "trace_id": execution.get("trace_ref"),
                           "business_status": execution.get("business_status"),
                           "model_id": execution.get("observed_version", {}).get("model_id"),
                           "tools_completeness": execution.get("tools_completeness"),
                           "context_scope": execution.get("context_scope"), "context_completeness": execution.get("context_completeness"),
                           "observation_status": execution.get("observation_status"), "observation_reason": execution.get("observation_reason"),
                           "usage_availability": execution.get("usage", {}).get("status"),
                           "model_attempt_status_counts": dict(Counter(item.get("status", "UNKNOWN") for item in execution.get("usage", {}).get("attempts", []))),
                           "provider_response_evidence_count": len(provider),
                           "provider_model_ids": sorted({item["model_id"] for item in provider}),
                           "requested_model_ids": sorted({item["requested_model_id"] for item in provider if item.get("requested_model_id")})})
            result["real_execution_gap_reason"] = (None if result["real_agent_verified"] else
                "NO_MODEL_REQUEST_DETERMINISTIC_BEHAVIOR" if observation.get("sourceType") == "NO_MODEL_REQUEST" and not execution.get("usage", {}).get("attempts") else
                "NO_SUCCESSFUL_PROVIDER_RESPONSE_METADATA")
            observation = execution.get("raw_agent", {}).get("observation")
            if isinstance(observation, dict):
                self.check(prefix + ":trace_conversation_turn_order_link", case.get("trace_id") == execution.get("trace_ref") == observation.get("traceId") and
                           execution.get("conversation_id") == observation.get("conversationId") and execution.get("turn_id") == observation.get("turnId") and
                           execution.get("order_id") == observation.get("orderId") and bool(execution.get("trace_ref")))
            else:
                self.check(prefix + ":missing_observation_explicitly_preserved", execution.get("observation_status") in {"UNAVAILABLE", "PENDING"} and bool(execution.get("observation_reason")), missing=True)
            tools, traces = execution.get("tools"), detail.get("traces", [])
            self.check(prefix + ":tool_traces_same_execution", len(traces) == len(tools or []) and all(
                trace.get("event_id") == tool.get("event_id") and trace.get("sequence") == tool.get("sequence") and
                trace.get("name") == tool.get("name") and trace.get("status") == tool.get("status") and
                trace.get("payload", {}).get("result") == tool.get("result") for trace, tool in zip(traces, tools or [])))
            self.check(prefix + ":unknown_tools_not_claimed_empty_complete", execution.get("tools_completeness") != "COMPLETE" or tools is not None)
            self.check(prefix + ":unique_ordered_tool_events", len({tool.get("event_id") for tool in tools or []}) == len(tools or []) and
                       [tool.get("sequence") for tool in tools or []] == sorted({tool.get("sequence") for tool in tools or []}))
        self.check(prefix + ":metric_reasons_and_unique_names", bool(metrics) and len({metric.get("metric") for metric in metrics}) == len(metrics) and
                   all(metric.get("metric") in QUALITY | RULES and metric.get("status") in {"PASS", "FAIL", "NA", "ERROR"} and
                       isinstance(metric.get("reason"), str) and bool(metric["reason"].strip()) for metric in metrics), missing=not metrics)
        required = [metric for metric in metrics if metric.get("required")]
        expected_conclusion = "FAIL" if any(metric.get("status") == "FAIL" for metric in required) else "UNDETERMINED" if not required or any(metric.get("status") in {"NA", "ERROR"} for metric in required) else "PASS"
        self.check(prefix + ":required_metrics_and_conclusion", {metric.get("metric") for metric in required} == set(frozen.get(key, {}).get("required_metrics", [])) and conclusion == expected_conclusion, missing=not metrics)
        for metric in metrics:
            if metric.get("metric") in QUALITY:
                score = metric.get("score")
                self.check(prefix + ":quality_score:" + metric["metric"], type(score) in {int, float} and int(score) == score and 1 <= score <= 5 if metric.get("status") in {"PASS", "FAIL"} else score is None)
                if metric["metric"] == "faithfulness" and metric.get("status") in {"PASS", "FAIL"}:
                    self.check(prefix + ":faithfulness_actual_complete_context", isinstance(execution, dict) and execution.get("context_scope") == "ACTUAL_MODEL_CONTEXT" and execution.get("context_completeness") == "COMPLETE" and bool(execution.get("contexts")))
        attempts = scoring.get("judge_attempts", [])
        self.check(prefix + ":bounded_ordered_judge_attempts", [attempt.get("attempt") for attempt in attempts] == list(range(1, len(attempts) + 1)) and len(attempts) <= 2)
        for attempt in attempts:
            request = unwrap(attempt.get("request")) or {}
            response = unwrap(attempt.get("response")) or {}
            number = str(attempt.get("attempt"))
            self.check(prefix + ":Judge:" + number + ":real_source", request.get("source_type") == "REAL_EXTERNAL")
            self.usage(prefix + ":Judge:" + number, attempt.get("usage"))
            try:
                messages = request["payload"]["messages"]
                data = json.loads(next(message["content"] for message in messages if message["role"] == "user"))["untrusted_evaluation_data"]
                self.check(prefix + ":Judge:" + number + ":same_frozen_input_answer", data["question"] == frozen[key]["question"] and
                           data["reference_behavior"] == frozen[key]["expected_answer"] and data["answer"] == case.get("answer"))
                self.check(prefix + ":Judge:" + number + ":same_judge_model", request["payload"]["model"] == run["snapshot"]["judge"]["model"])
            except (KeyError, ValueError, StopIteration, TypeError):
                self.check(prefix + ":Judge:" + number + ":request_recoverable", False, missing=True)
            raw = unwrap(response.get("raw")) or {}
            if attempt.get("status") == "SUCCEEDED":
                try:
                    envelope = json.loads(raw["body"])
                    returned = json.loads(envelope["choices"][0]["message"]["content"])["scores"]
                    parsed = response["parsed"]
                    self.check(prefix + ":Judge:" + number + ":raw_response_matches_parsed", raw.get("http_status") == 200 and returned == parsed and bool(parsed))
                    by_metric = {metric["metric"]: metric for metric in metrics}
                    self.check(prefix + ":Judge:" + number + ":parsed_matches_saved_metrics", all(by_metric[name]["score"] == value["score"] and by_metric[name]["reason"] == value["reason"] for name, value in parsed.items()))
                    result["real_judge_success"] = request.get("source_type") == "REAL_EXTERNAL" and returned == parsed and raw.get("http_status") == 200
                except (KeyError, ValueError, TypeError, IndexError):
                    self.check(prefix + ":Judge:" + number + ":raw_response_recoverable", False, missing=True)
            else:
                self.check(prefix + ":Judge:" + number + ":failure_reason_retained", bool(attempt.get("error")))
        supported = isinstance(execution, dict) and execution.get("agent_status") == "SUCCEEDED" and bool(execution.get("answer"))
        if supported:
            self.check(prefix + ":supported_quality_has_real_judge", result["real_judge_success"], missing=True)
        result["judge_attempts"] = len(attempts)
        result["metric_statuses"] = {metric["metric"]: metric.get("status") for metric in metrics if "metric" in metric}
        result["quality_failures"] = [metric["metric"] for metric in metrics if metric.get("metric") in QUALITY and metric.get("status") == "FAIL"]
        result["audit_status"] = status_of(self.checks[start:])
        self.case_audits.append(result)
        return result

    def run(self, variant, row):
        run, report, details = row["run"], row["report"], row["cases"]
        snapshot, summary = run["snapshot"], report["summary"]
        frozen = {case["case_key"]: case for case in snapshot["cases"]}
        score_id = report["score_set"]["id"]
        self.check(variant + ":fixed_30_cases", run.get("total_cases") == 30 and summary.get("N") == 30 and len(snapshot["cases"]) == len(frozen) == len(details) == 30)
        self.check(variant + ":terminal_run", run.get("status") in TERMINAL)
        self.check(variant + ":batch_completed", run.get("status") == "COMPLETED", missing=True, actual_status=run.get("status"))
        self.check(variant + ":snapshot_sha256_matches", digest(snapshot) == run.get("snapshot_sha256"))
        self.check(variant + ":report_and_run_same_snapshot_score", report["run"]["id"] == run["id"] and report_snapshot_matches(report["run"]["snapshot"], snapshot) and
                   report["score_set"]["run_id"] == run["id"] and run.get("score_set_id") == score_id and report["score_set"].get("kind") == "ORIGINAL")
        self.check(variant + ":frozen_scoring_fingerprint", report["score_set"]["judge_snapshot"] == snapshot["judge"] and report["score_set"]["rule_snapshot"] == snapshot["rules"] and
                   report["score_set"]["fingerprint"] == digest({"judge": snapshot["judge"], "rules": snapshot["rules"]}))
        keys = [detail.get("case", {}).get("case_key") for detail in details]
        self.check(variant + ":all_frozen_case_keys_once", len(set(keys)) == 30 and set(keys) == set(frozen))
        audited = [self.case(variant, detail, run, frozen, score_id) for detail in details]
        self.check(variant + ":distinct_case_execution_and_trace_identities", len({case["case_run_id"] for case in audited}) == 30 and all(
            len(values) == len(set(values)) for values in ([case.get(field) for case in audited if case.get(field)] for field in ("execution_id", "trace_id"))))
        counts = Counter(case["conclusion"] for case in audited)
        p, f, u = counts["PASS"], counts["FAIL"], counts["UNDETERMINED"]
        self.check(variant + ":PFU_recomputed", p + f + u == 30 and all(summary.get(key) == value for key, value in {"P": p, "F": f, "U": u}.items()))
        self.check(variant + ":rates_have_fixed_denominators", near(summary.get("effective_pass_rate"), ratio(p, p + f)) and near(summary.get("determinate_coverage"), ratio(p + f, 30)))
        for metric in QUALITY:
            values = [item["score"] for detail in details for item in detail.get("scoring", {}).get("metrics", []) if item.get("metric") == metric and item.get("status") in {"PASS", "FAIL"}]
            saved = summary.get("metric_averages", {}).get(metric, {})
            self.check(variant + ":mean_and_valid_count:" + metric, saved.get("valid_count") == len(values) and saved.get("total_count") == 30 and near(saved.get("mean"), sum(values) / len(values) if values else None))
        for kind in ("Agent", "Judge"):
            usage = summary.get("usage", {}).get(kind, {})
            self.check(variant + ":" + kind + ":unpriced_cost_not_zero", usage.get("price_configuration") == [] and usage.get("cost") is None and usage.get("cost_reason") == "NO_PRICE_CONFIGURATION")
            usage_rows = []
            def add_usage(item):
                if item.get("status") == "NOT_REQUESTED":
                    return
                usage_rows.extend(item.get("attempts") or [item])
            if kind == "Agent":
                seen = set()
                def collect_execution(item):
                    identity = item.get("execution_id")
                    if identity in seen:
                        return
                    seen.add(identity)
                    for prior in item.get("prerequisites", []):
                        collect_execution(prior)
                    if not item.get("replayed"):
                        add_usage(item.get("usage") or {})
                for detail in details:
                    execution = (detail.get("case", {}).get("raw_agent") or {}).get("normalized_execution")
                    if isinstance(execution, dict):
                        collect_execution(execution)
            else:
                for detail in details:
                    for attempt in detail.get("scoring", {}).get("judge_attempts", []):
                        add_usage(attempt.get("usage") or {})
            for counter in COUNTERS:
                known = [item[counter] for item in usage_rows if item.get(counter) is not None]
                self.check(variant + ":" + kind + ":known_usage_recomputed:" + counter,
                           usage.get(counter) == (sum(known) if known else None) and usage.get("known_" + counter + "_records") == len(known))
        scope = summary.get("source_scope", {})
        real_agent = sum(case["real_agent_verified"] for case in audited)
        real_judge = sum(case["real_judge_success"] for case in audited)
        self.check(variant + ":real_coverage_not_overstated", scope.get("model_verified_count") == real_agent and scope.get("real_judge_success_count") == real_judge and
                   scope.get("contains_simulation") is False and (scope.get("real_quality_evidence") is not True or real_agent == real_judge == 30))
        self.run_audits[variant] = {"run_id": run["id"], "score_set_id": score_id, "status": run["status"], "N": 30, "P": p, "F": f, "U": u,
            "snapshot_sha256": run["snapshot_sha256"], "case_content_sha256": digest({key: {k: v for k, v in case.items() if k != "revision"} for key, case in frozen.items()}),
            "judge_sha256": digest(snapshot["judge"]), "rules_sha256": digest(snapshot["rules"]),
            "real_agent_verified_count": real_agent, "real_judge_success_count": real_judge,
            "failed_or_unknown_cases": [{key: case[key] for key in ("case_key", "agent_status", "conclusion", "explicit_error", "evaluation_issues", "audit_status")} for case in audited if case["conclusion"] != "PASS" or case["audit_status"] != "PASS"],
            "metric_averages": summary.get("metric_averages"), "usage": summary.get("usage"), "version_verification": report.get("version_verification")}

    def compare(self, evidence):
        first, second = self.run_audits["A"], self.run_audits["B"]
        comparison = evidence["comparison"]
        self.check("AB:independent_runs_and_scores", first["run_id"] != second["run_id"] and first["score_set_id"] != second["score_set_id"])
        self.check("AB:no_execution_or_trace_reused_between_versions", all(
            not ({case.get(field) for case in self.case_audits if case["variant"] == "A" and case.get(field)} &
                 {case.get(field) for case in self.case_audits if case["variant"] == "B" and case.get(field)})
            for field in ("case_run_id", "execution_id", "trace_id")))
        same = all(first[key] == second[key] for key in ("case_content_sha256", "judge_sha256", "rules_sha256"))
        self.check("AB:same_frozen_cases_judge_rules", same)
        self.check("AB:comparison_conditions_correct", comparison.get("comparable") is same and same and not comparison.get("limitations"))
        self.check("AB:comparison_same_run_score_scope", comparison["left"]["run"]["id"] == first["run_id"] and comparison["right"]["run"]["id"] == second["run_id"] and
                   comparison["left"]["score_set"]["id"] == first["score_set_id"] and comparison["right"]["score_set"]["id"] == second["score_set_id"])
        versions = comparison.get("version_comparison", {})
        model = next((dimension for dimension in versions.get("dimensions", []) if dimension.get("dimension") == "model_id"), {})
        self.check("AB:actual_model_change_verified", versions.get("verified_change") is True and model.get("verified") is True and model.get("changed") is True and
                   bool(model.get("left")) and bool(model.get("right")) and model["left"] != model["right"], missing=True)
        for variant, side in (("A", "left"), ("B", "right")):
            actual_models = sorted({case["model_id"] for case in self.case_audits if case["variant"] == variant and case["real_agent_verified"] and case.get("model_id")})
            self.check("AB:" + variant + ":model_change_backed_by_actual_metadata", bool(actual_models) and actual_models == sorted(model.get(side, [])), missing=not actual_models)
        audited_left = {case["case_key"]: case for case in self.case_audits if case["variant"] == "A"}
        audited_right = {case["case_key"]: case for case in self.case_audits if case["variant"] == "B"}
        for key in sorted(set(audited_left) & set(audited_right)):
            left, right = audited_left[key], audited_right[key]
            if left["real_agent_verified"] and right["real_agent_verified"] and left.get("model_id") != right.get("model_id"):
                self.successful_pair_model_changes.append({"case_key": key, "left_case_run_id": left["case_run_id"],
                    "right_case_run_id": right["case_run_id"], "left_trace_id": left.get("trace_id"), "right_trace_id": right.get("trace_id"),
                    "left_observed_model": left.get("model_id"), "right_observed_model": right.get("model_id"),
                    "left_requested_models": left.get("requested_model_ids"), "right_requested_models": right.get("requested_model_ids"),
                    "left_provider_models": left.get("provider_model_ids"), "right_provider_models": right.get("provider_model_ids")})
        left_cases = {detail["case"]["case_key"]: detail for detail in evidence["runs"]["A"]["cases"]}
        right_cases = {detail["case"]["case_key"]: detail for detail in evidence["runs"]["B"]["cases"]}
        for metric in comparison.get("metrics", []):
            name = metric.get("metric")
            pairs = metric.get("pairs", [])
            self.check("AB:pair_count:" + str(name), metric.get("paired_count") == len(pairs))
            self.check("AB:empty_or_incomparable_delta_null:" + str(name), bool(pairs) and same or metric.get("delta") is None)
            if name in QUALITY:
                expected = []
                for key in sorted(set(left_cases) & set(right_cases)):
                    left = next((item for item in left_cases[key]["scoring"]["metrics"] if item["metric"] == name and item["status"] in {"PASS", "FAIL"}), None)
                    right = next((item for item in right_cases[key]["scoring"]["metrics"] if item["metric"] == name and item["status"] in {"PASS", "FAIL"}), None)
                    if left and right:
                        expected.append((key, left["score"], right["score"]))
                self.check("AB:common_valid_quality_pairs:" + name, metric.get("paired_count") == len(expected) and {item.get("case_key") for item in pairs} == {item[0] for item in expected})
                delta = sum(right - left for _, left, right in expected) / len(expected) if same and expected else None
                self.check("AB:quality_delta_recomputed:" + name, near(metric.get("delta"), delta))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "docs/evidence/g7-real-ab-20261116.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/evidence/g7-results-audit-20261116.json")
    args = parser.parse_args()
    source, output = args.input.resolve(), args.output.resolve()
    if output.exists() or source == output:
        raise SystemExit("OUTPUT_EXISTS_OR_MATCHES_INPUT_NO_OVERWRITE")
    audit = Audit()
    input_hash = None
    if not source.exists():
        audit.check("input_artifact_available", False, missing=True)
    else:
        blob = source.read_bytes()
        input_hash = hashlib.sha256(blob).hexdigest()
        try:
            evidence = json.loads(blob)
            for variant in ("A", "B"):
                try:
                    audit.run(variant, evidence["runs"][variant])
                except (KeyError, TypeError, ValueError, IndexError) as failure:
                    audit.check(variant + ":required_evidence_schema_complete", False, missing=True, error_type=type(failure).__name__)
            if set(audit.run_audits) == {"A", "B"}:
                audit.compare(evidence)
            else:
                audit.check("AB:both_run_audits_available", False, missing=True)
            audit.check("source_worktree_preserved", evidence.get("source_worktree_unchanged") is True, missing="source_worktree_unchanged" not in evidence)
            audit.check("data_files_preserved", evidence.get("data_files_unchanged") is True, missing="data_files_unchanged" not in evidence)
            audit.check("input_artifact_unchanged_during_read", hashlib.sha256(source.read_bytes()).hexdigest() == input_hash)
        except (KeyError, TypeError, ValueError, IndexError) as failure:
            audit.check("input_evidence_recoverable", False, missing=True, error_type=type(failure).__name__)
    status = status_of(audit.checks)
    payload = {"schema": "agenteval-group7-results-audit-v1", "status": status,
        "structural_integrity_status": status_of([row for row in audit.checks if row["category"] == "STRUCTURAL_INTEGRITY"]),
        "real_coverage_status": status_of([row for row in audit.checks if row["category"] == "REAL_COVERAGE"]),
        "audited_at_utc": datetime.now(timezone.utc).isoformat(), "input_file": str(source), "input_sha256": input_hash,
        "auditor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "operation_scope": "READ_SAVED_JSON_ONLY_NO_HTTP_DATABASE_OR_MODEL_CALLS",
        "external_agent_calls": 0, "external_judge_calls": 0,
        "checks_summary": dict(Counter(row["status"] for row in audit.checks)), "checks": audit.checks,
        "runs": audit.run_audits, "case_audits": audit.case_audits,
        "successful_pair_model_change_evidence": {"count": len(audit.successful_pair_model_changes), "total_frozen_pairs": 30,
            "scope": "ONLY_PAIRS_WITH_SUCCESSFUL_PROVIDER_RESPONSE_METADATA_ON_BOTH_SIDES_DOES_NOT_UPGRADE_WHOLE_BATCH_VERSION_VERIFICATION",
            "pairs": audit.successful_pair_model_changes},
        "retained_limitations": ["QUALITY_FAIL_IS_PRESERVED_AND_DOES_NOT_MEAN_AUDIT_FAILURE", "EXPECTED_NA_FAITHFULNESS_NOT_FILLED_WITH_ZERO",
            "MISSING_SUCCESSFUL_REAL_JUDGE_OR_MODEL_EVIDENCE_IS_UNVERIFIED", "SYNTHETIC_SEEN_DATA_AI_AUTHORED_AI_REVIEWED",
            "SUPPLIER_IMMUTABLE_MODEL_SNAPSHOT_UNKNOWN", "NO_HUMAN_QUALITY_OR_AUTHOR_REHEARSAL_VERIFIED",
            "RUNS_ENDPOINT_SNAPSHOT_USED_FOR_ORIGINAL_HASH_REPORT_PASSWORD_REF_IS_REDACTED_PROJECTION",
            "SOURCE_EXPORT_SHA_IDENTIFIES_ARTIFACT_NOT_INDEPENDENT_PROVIDER_ATTESTATION"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"status": status, "checks_summary": payload["checks_summary"], "cases": len(audit.case_audits), "output": str(output)}, ensure_ascii=True))
    raise SystemExit(0 if status == "PASS" else 1)


if __name__ == "__main__":
    main()
