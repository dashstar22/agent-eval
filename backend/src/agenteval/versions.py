"""把候选声明与逐条实际观测分开；标签和外部模型别名不能充当构建证明。"""

import re

from .snapshots import canonical_json


DIMENSIONS = ("build_sha256", "prompt_sha256", "model_id", "knowledge_version")
UNKNOWN_VALUES = {"", "UNKNOWN", "UNAVAILABLE", "NONE", "NULL", "NOT_PROVIDED"}


def _known(value):
    return value is not None and str(value).strip().upper() not in UNKNOWN_VALUES


def _declared_knowledge(version):
    values = []
    for item in version.knowledge_versions:
        value = item.get("version") if isinstance(item, dict) else item
        if _known(value):
            values.append(str(value))
    return set(values)


def _observed(execution, dimension):
    if execution is None:
        return []
    version = execution.observed_version
    if dimension == "prompt_sha256":
        # 主模板摘要用于候选核验；逐次请求的其他摘要仍在原观测中，不能混作同一预期值。
        return [version.prompt_sha256] if _known(version.prompt_sha256) else []
    if dimension == "knowledge_version":
        values = _declared_knowledge(version)
        # 实际上下文中已知的其他版本也必须展示，不能由不完整的声明列表隐藏。
        if execution.context_scope == "ACTUAL_MODEL_CONTEXT":
            values.update(str(item.version) for item in execution.contexts or [] if _known(item.version))
        return sorted(values)
    value = getattr(version, dimension)
    return [value] if _known(value) else []


def _verified(execution, dimension):
    if execution is None or execution.source_type != "REAL_EXTERNAL":
        return False
    if dimension == "build_sha256":
        return bool(re.fullmatch(r"[a-fA-F0-9]{64}", execution.observed_version.build_sha256 or "")) and execution.observed_version.build_status in {"VERIFIED", "VERIFIED_PROCESS_JAR"}
    if not execution.model_execution_verified:
        return False
    version = execution.observed_version
    provider_attempts = [item for item in execution.usage.attempts if item.status == "SUCCEEDED" and item.source == "MODEL_RESPONSE_METADATA"]
    if dimension == "prompt_sha256":
        return bool(re.fullmatch(r"[a-fA-F0-9]{64}", version.prompt_sha256 or "")) and (version.verification == "VERIFIED" or any(item.prompt_sha256 == version.prompt_sha256 for item in provider_attempts))
    if dimension == "model_id":
        return any(version.model_id in {item.model_id, item.requested_model_id} for item in provider_attempts)
    contexts = execution.contexts or []
    context_versions = {str(item.version) for item in contexts if _known(item.version)}
    return (execution.context_scope == "ACTUAL_MODEL_CONTEXT" and execution.context_completeness == "COMPLETE"
            and bool(contexts) and all(_known(item.version) for item in contexts)
            and bool(context_versions) and _declared_knowledge(version) == context_versions)


def verify_versions(snapshot, cases):
    expected = snapshot.get("agent", {}).get("expected_version", {})
    dimensions, limitations = [], []
    for dimension in DIMENSIONS:
        observations, verified_count, observed_count = {}, 0, 0
        for case in cases:
            values = _observed(case.execution, dimension)
            if values:
                observed_count += 1
                verified_count += int(_verified(case.execution, dimension))
            for value in values:
                observations[canonical_json(value)] = value
        observed = [observations[key] for key in sorted(observations)]
        expectation = expected.get(dimension) if _known(expected.get(dimension)) else None
        if not observed:
            status, reason = "UNKNOWN", "ACTUAL_VERSION_UNAVAILABLE"
        elif expectation is not None and any(str(value) != str(expectation) for value in observed):
            status, reason = "MISMATCH", "OBSERVED_VERSION_DIFFERS_FROM_EXPECTED"
        elif verified_count != observed_count:
            status, reason = "UNVERIFIED", "OBSERVATION_NOT_VERIFIED_REAL_EXECUTION"
        elif observed_count != len(cases):
            status, reason = "PARTIAL", "SOME_CASE_VERSIONS_UNAVAILABLE"
        elif expectation is not None:
            status, reason = "MATCH", None
        else:
            status, reason = "OBSERVED", "NO_EXPECTED_VERSION_CONFIGURED"
        dimensions.append({"dimension": dimension, "expected": expectation, "observed": observed,
                           "status": status, "verified_count": verified_count, "observed_count": observed_count,
                           "verification": "VERIFIED" if observed_count and verified_count == observed_count else "UNVERIFIED",
                           "expected_match": None if expectation is None or not observed else all(str(value) == str(expectation) for value in observed),
                           "total_count": len(cases), "reason": reason})
    if any(item["status"] == "MISMATCH" for item in dimensions):
        status = "MISMATCH"
    elif all(item["status"] in {"MATCH", "OBSERVED"} for item in dimensions):
        status = "VERIFIED"
    else:
        status = "INCOMPLETE"
    limitations.extend(["CANDIDATE_LABEL_IS_NOT_VERSION_EVIDENCE", "SUPPLIER_IMMUTABLE_MODEL_SNAPSHOT_UNKNOWN",
                        "KNOWLEDGE_VERSIONS_DESCRIBE_OBSERVED_EVIDENCE_NOT_WHOLE_CORPUS"])
    return {"candidate_label": snapshot.get("candidate_version_label", ""), "status": status,
            "dimensions": dimensions, "model_snapshot_status": "UNKNOWN", "limitations": limitations}


def compare_versions(left, right):
    dimensions = []
    verified_change = False
    for first, second in zip(left["dimensions"], right["dimensions"]):
        trusted = all(item["status"] in {"MATCH", "OBSERVED"} for item in (first, second))
        changed = bool(first["observed"] and second["observed"] and first["observed"] != second["observed"])
        verified_change |= trusted and changed
        dimensions.append({"dimension": first["dimension"], "left": first["observed"], "right": second["observed"],
                           "changed": changed, "verified": trusted and changed,
                           "status": "VERIFIED_CHANGE" if trusted and changed else "SAME_OBSERVED" if trusted else "UNVERIFIED"})
    limits = ["SINGLE_RUN_DIFFERENCE_IS_NOT_STATISTICAL_SIGNIFICANCE", "SUPPLIER_IMMUTABLE_MODEL_SNAPSHOT_UNKNOWN"]
    if not verified_change:
        limits.append("NO_VERIFIED_ACTUAL_CONFIGURATION_CHANGE")
    return {"verified_change": verified_change, "status": "VERIFIED_CHANGE" if verified_change else "UNVERIFIED_CHANGE",
            "dimensions": dimensions, "limitations": limits}
