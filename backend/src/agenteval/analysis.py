"""回归、稳定性与离线导出；只读保存结果，永不调用模型。"""

from collections import Counter
import csv
from html import escape
from io import StringIO
import json
from math import fsum, isclose, sqrt
from urllib.parse import urlencode

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from .analysis_schemas import RegressionCheckInput, StabilityAnalysisInput
from .comparisons import COMPARISON_METRICS, _value, compare_runs, comparison_side
from .errors import ApiError
from .persistence import CaseRun
from .privacy import known_secrets, sanitize
from .schemas import METRICS, RuleProfile
from .snapshots import digest
from .versions import verify_versions
from .workbench import detail, report

ANALYSIS_SCOPE = "SAVED_RESULTS_ONLY_NO_NEW_AGENT_OR_JUDGE_CALLS"


def _safe_result(value, sides):
    refs = {}
    for index, (run, score, *_) in enumerate(sides):
        refs.update({f"agent_{index}_{key}": value for key, value in run.snapshot.get("agent", {}).get("credential_refs", {}).items()})
        refs[f"judge_{index}"] = score.judge_snapshot.get("credential_ref")
    return sanitize(value, known_secrets(refs))


def _case_link(run_id, score_id, case_id):
    return "/evaluations?" + urlencode({"run_id": run_id, "score_set_id": score_id, "case_run_id": case_id})


def regression_check(session, data: RegressionCheckInput):
    """在共同有效题目上检查回归；缺数和不可比不会通过门禁。"""
    comparison = compare_runs(session, data.baseline.run_id, data.candidate.run_id,
                              data.baseline.score_set_id, data.candidate.score_set_id)
    baseline = comparison_side(session, data.baseline.run_id, data.baseline.score_set_id)
    candidate = comparison_side(session, data.candidate.run_id, data.candidate.score_set_id)
    left, right = comparison["left"], comparison["right"]
    completed = all(side["run"]["status"] == "COMPLETED" and side["score_set"]["status"] == "COMPLETED" for side in (left, right))
    policy = data.policy
    rate = next(item for item in comparison["metrics"] if item["metric"] == "pass_rate")
    coverages = [left["summary"]["determinate_coverage"], right["summary"]["determinate_coverage"], rate["paired_coverage"]]
    coverage_ok = all(value is not None and value >= policy.min_coverage for value in coverages)
    limits = list(comparison["limitations"])
    if not completed:
        limits.append("RUN_OR_SCORE_SET_NOT_COMPLETED")
    if not coverage_ok:
        limits.append("INSUFFICIENT_DETERMINATE_OR_PAIRED_COVERAGE")
    if not rate["paired_count"]:
        limits.append("NO_COMMON_VALID_CONCLUSIONS")
    eligible = completed and comparison["comparable"] and coverage_ok and rate["paired_count"] > 0
    new_failures, fixed_cases, unknown_cases = [], [], []
    for item in comparison["cases"]:
        row = dict(item)
        row["baseline_link"] = _case_link(left["run"]["id"], left["score_set"]["id"], item["left_case_run_id"]) if item["left_case_run_id"] else None
        row["candidate_link"] = _case_link(right["run"]["id"], right["score_set"]["id"], item["right_case_run_id"]) if item["right_case_run_id"] else None
        first, second = item["left_conclusion"], item["right_conclusion"]
        if not item["content_match"] or first not in {"PASS", "FAIL"} or second not in {"PASS", "FAIL"}:
            unknown_cases.append(row)
        elif first == "PASS" and second == "FAIL":
            new_failures.append(row)
        elif first == "FAIL" and second == "PASS":
            fixed_cases.append(row)
    drop = max(0.0, -rate["delta"]) if rate["delta"] is not None else None
    # 默认零下降保持严格；正阈值容忍二进制浮点减法的舍入误差。
    drop_within_policy = drop is not None and (drop <= policy.max_pass_rate_drop or
        (policy.max_pass_rate_drop > 0 and isclose(drop, policy.max_pass_rate_drop, rel_tol=0, abs_tol=1e-12)))
    checks = [
        {"id": "completed", "label": "运行与评分集已完成", "status": "PASS" if completed else "INCONCLUSIVE", "actual": int(completed), "threshold": 1, "reason": None if completed else "RUN_OR_SCORE_SET_NOT_COMPLETED"},
        {"id": "comparable", "label": "数据、Judge 与规则口径一致", "status": "PASS" if comparison["comparable"] else "INCONCLUSIVE", "actual": int(comparison["comparable"]), "threshold": 1, "reason": None if comparison["comparable"] else "CONDITIONS_MISMATCH"},
        {"id": "coverage", "label": "两侧及共同有效结论覆盖率", "status": "PASS" if coverage_ok and rate["paired_count"] else "INCONCLUSIVE", "actual": min(coverages) if all(value is not None for value in coverages) else None, "threshold": policy.min_coverage, "reason": None if coverage_ok and rate["paired_count"] else "INSUFFICIENT_COVERAGE_OR_NO_VALID_PAIRS"},
        {"id": "new_failures", "label": "已知通过变失败用例数", "status": ("PASS" if len(new_failures) <= policy.max_new_failures else "FAIL") if eligible else "INCONCLUSIVE", "actual": len(new_failures) if comparison["comparable"] else None, "threshold": policy.max_new_failures, "reason": None if eligible else "PREREQUISITES_NOT_SATISFIED"},
        {"id": "pass_rate_drop", "label": "共同有效用例通过率下降", "status": ("PASS" if drop_within_policy else "FAIL") if eligible else "INCONCLUSIVE", "actual": drop, "threshold": policy.max_pass_rate_drop, "reason": None if eligible else "PREREQUISITES_NOT_SATISFIED"},
    ]
    verdict = "INCONCLUSIVE" if not eligible else "FAIL" if any(item["status"] == "FAIL" for item in checks) else "PASS"
    result = {"verdict": verdict, "policy": policy.model_dump(), "scope": ANALYSIS_SCOPE,
              "conditions": {"completed": completed, "comparable": comparison["comparable"], "coverage_sufficient": coverage_ok,
                             "limitations": limits, "version_evidence": {"baseline": verify_versions(baseline[0].snapshot, baseline[2]),
                                "candidate": verify_versions(candidate[0].snapshot, candidate[2]), "comparison": comparison["version_comparison"]},
                             "source_scope": {"baseline": left["summary"]["source_scope"], "candidate": right["summary"]["source_scope"]}},
              "checks": checks, "new_failures": new_failures, "fixed_cases": fixed_cases, "unknown_cases": unknown_cases,
              "comparison": comparison,
              "limitations": ["门禁只描述所选保存结果及策略，不证明统计显著性、线上效果或完整真实质量。"]}
    return _safe_result(result, (baseline, candidate))


def _stats(values, total, reason=None):
    n = len(values)
    mean = fsum(values) / n if n else None
    return {"valid_count": n, "total_count": total, "mean": mean,
            "stddev": sqrt(fsum((value - mean) ** 2 for value in values) / n) if n > 1 else None,
            "min": min(values) if values else None, "max": max(values) if values else None,
            "reason": reason or ("NO_VALID_OBSERVATIONS" if not n else "SINGLE_VALID_OBSERVATION_NO_DISPERSION" if n == 1 else None)}


def _condition_fingerprints(run, score):
    snapshot = run.snapshot
    # 身份、候选标签和创建时间不是运行条件；输入配置、修订和预算则必须相等。
    return {"dataset": digest(snapshot.get("dataset")),
            "cases": digest(sorted(snapshot.get("cases", []), key=lambda item: item["case_key"])),
            "agent": digest(snapshot.get("agent")), "judge": digest(dict(score.judge_snapshot)),
            "rules": digest(dict(score.rule_snapshot)), "budget": digest(snapshot.get("execution_budget")),
            "components": digest({"schema_version": snapshot.get("schema_version"), "components": snapshot.get("components")})}


def stability_analysis(session, data: StabilityAnalysisInput):
    sides = [comparison_side(session, item.run_id, item.score_set_id) for item in data.selections]
    # 防御直接 Python 调用方绕过请求验证，以及错误地把 RESCORE 当第二轮执行。
    if len({run.id for run, *_ in sides}) != len(sides):
        raise ApiError(422, "DUPLICATE_EXECUTION_SELECTION", "稳定性分析须选择不同实际运行")
    first = _condition_fingerprints(sides[0][0], sides[0][1])
    limits = []
    for dimension in first:
        if any(_condition_fingerprints(run, score)[dimension] != first[dimension] for run, score, *_ in sides[1:]):
            limits.append(dimension.upper() + "_FINGERPRINT_MISMATCH")
    conditions_match = not limits
    completed = all(run.status == "COMPLETED" and score.status == "COMPLETED" for run, score, *_ in sides)
    if not completed:
        limits.append("RUN_OR_SCORE_SET_NOT_COMPLETED")
    comparable = conditions_match and completed
    runs, case_maps, ids = [], [], []
    for run, score, cases, side in sides:
        versions = verify_versions(run.snapshot, cases)
        runs.append({"run_id": run.id, "score_set_id": score.id, "run_status": run.status, "score_set_status": score.status,
                     "kind": score.kind, "summary": side["summary"], "source_scope": side["summary"]["source_scope"], "version_verification": versions})
        case_maps.append({item.case_key: item for item in cases})
        ids.append(dict(session.execute(select(CaseRun.case_key, CaseRun.id).where(CaseRun.run_id == run.id)).all()))
    keys = sorted({item["case_key"] for run, *_ in sides for item in run.snapshot["cases"]})
    cases = []
    for key in keys:
        observations = []
        for index, (run, score, *_rest) in enumerate(sides):
            item = case_maps[index].get(key)
            observations.append({"run_id": run.id, "score_set_id": score.id, "case_run_id": ids[index].get(key),
                                 "conclusion": item.assessment.conclusion if item and item.assessment else "UNDETERMINED",
                                 "agent_status": item.execution.agent_status if item and item.execution else None})
        counts = Counter(item["conclusion"] for item in observations)
        known = [item["conclusion"] for item in observations if item["conclusion"] in {"PASS", "FAIL"}]
        flips = sum(a != b for a, b in zip(known, known[1:])) if len(known) >= 2 else None
        cases.append({"case_key": key, "P": counts["PASS"], "F": counts["FAIL"], "U": counts["UNDETERMINED"],
                      "valid_count": len(known), "total_count": len(sides), "flipped": bool(flips) if comparable and flips is not None else None,
                      "flip_count": flips if comparable else None, "observations": observations,
                      "links": [_case_link(row["run_id"], row["score_set_id"], row["case_run_id"]) for row in observations if row["case_run_id"]]})
    metrics = []
    metric_names = list(dict.fromkeys((*COMPARISON_METRICS, *sorted(METRICS))))
    for metric in metric_names:
        values, reasons = [], Counter()
        for index, (_, score, *_rest) in enumerate(sides):
            profile = RuleProfile.model_validate(dict(score.rule_snapshot))
            for key in keys:
                item = case_maps[index].get(key)
                # 新规则指标由原始状态读取；不改变既有 comparison 模块合同。
                if metric in METRICS and metric not in COMPARISON_METRICS:
                    saved = next((m for m in item.assessment.metrics if m.metric == metric), None) if item and item.assessment else None
                    value, reason = (int(saved.status == "PASS"), None) if saved and saved.status in {"PASS", "FAIL"} else (None, "METRIC_UNAVAILABLE")
                else:
                    value, reason = _value(item, metric, profile)
                if value is not None:
                    values.append(value)
                else:
                    reasons[reason] += 1
        stats = _stats(values, len(keys) * len(sides))
        if not comparable:
            stats.update({"mean": None, "stddev": None, "min": None, "max": None, "reason": "CONDITIONS_MISMATCH_OR_INCOMPLETE"})
        metrics.append({"metric": metric, **stats, "unavailable_reasons": dict(reasons), "aggregation": "ALL_VALID_CASE_EXECUTION_OBSERVATIONS", "stddev_kind": "POPULATION"})
    return _safe_result({"scope": ANALYSIS_SCOPE, "comparable": comparable, "limitations": limits,
                         "run_count": len(sides), "runs": runs, "cases": cases, "metrics": metrics,
                         "conditions": {"frozen_conditions_match": conditions_match, "completed": completed,
                                        "actual_versions_verified": all(row["version_verification"]["status"] == "VERIFIED" for row in runs)},
                         "notes": ["按请求所列运行顺序比较已知结论；跳过未判定并保留其数量。", "标准差为有效逐题执行观测的总体标准差；单个有效值不报告标准差。", "相同冻结配置不代表供应商不可变模型快照相同。"]}, sides)


def export_document(session, run_id, score_set_id=None):
    """完整的单评分集脱敏报告快照，包括所有逐题证据。"""
    from .reviews import review_history

    side = comparison_side(session, run_id, score_set_id)
    run, score = side[:2]
    case_ids = list(session.scalars(select(CaseRun.id).where(CaseRun.run_id == run.id).order_by(CaseRun.case_key)))
    reviews = review_history(session, run.id, score.id)
    latest = {item["case_run_id"]: item for item in reviews["latest_reviews"]}
    cases = []
    for case_id in case_ids:
        item = detail(session, case_id, score.id)
        item["human_reviews"] = [row for row in reviews["items"] if row["case_run_id"] == case_id]
        item["latest_human_review"] = latest.get(case_id)
        cases.append(item)
    value = {"schema_version": "agenteval-offline-report-v1", "scope": ANALYSIS_SCOPE,
             "report": report(session, run.id, score.id), "cases": cases, "human_reviews": reviews,
             "limitations": ["仅导出选定运行和评分集的已保存结果，不产生新评分。", "来源、实际版本、缺失用量和观测完整性以保存记录为准；模拟与协议替身不是真实质量证明。"]}
    return jsonable_encoder(_safe_result(value, (side,)))


def _csv_cell(value):
    text = "" if value is None else value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    # Excel/LibreOffice 可忽略开头的空格或控制字符，先检查去掉它们后的首字符。
    stripped = text.lstrip(" \t\r\n\v\f\ufeff")
    return "'" + text if stripped.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def _export_csv(document):
    out = StringIO(newline="")
    fields = ["run_id", "score_set_id", "score_set_kind", "case_run_id", "case_key", "question", "expected_answer", "answer", "agent_status", "conclusion", "judge_status", "source_type", "model_execution_verified", "automatic_tags", "evaluation_issues", "metrics", "manual_tags", "manual_note", "observations", "usage", "timing", "error", "traces", "judge_attempts", "version_verification", "human_reviews", "latest_human_review"]
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(fields)
    for item in document["cases"]:
        case, score, scoring = item["case"], item["score_set"], item["scoring"]
        normalized = (case.get("raw_agent") or {}).get("normalized_execution") or {}
        evaluation = scoring["evaluation"] or {}
        values = [case["run_id"], score["id"], score["kind"], case["id"], case["case_key"], case["case_snapshot"].get("question"), case["case_snapshot"].get("expected_answer"), case.get("answer"), case.get("agent_status"), evaluation.get("conclusion", "UNDETERMINED"), evaluation.get("status"), normalized.get("source_type"), normalized.get("model_execution_verified"), item["automatic_tags"], item["evaluation_issues"], scoring["metrics"], item["annotation"]["manual_tags"], item["annotation"]["note"], case.get("observations"), case.get("usage"), case.get("timing"), case.get("error"), item["traces"], scoring["judge_attempts"], item["version_verification"], item["human_reviews"], item["latest_human_review"]]
        writer.writerow([_csv_cell(value) for value in values])
    return "\ufeff" + out.getvalue()


def _export_html(document):
    def pretty(value):
        return escape(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), quote=True)
    value = document["report"]
    summary = value["summary"]
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             '<title>AgentEval 离线评测报告</title><style>body{font:14px/1.7 system-ui,sans-serif;color:#172b4d;background:#f3f6fa;margin:0}main{max-width:1100px;margin:auto;padding:24px}section{background:white;border:1px solid #d9e2ec;border-radius:8px;padding:20px;margin:20px 0}h1{font-size:28px}h2{font-size:18px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 monospace;background:#f8fafc;padding:14px}dt{font-weight:600}dd{margin:0 0 12px;white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}table{border-collapse:collapse;width:100%}th,td{padding:8px;text-align:left;border-bottom:1px solid #d9e2ec;overflow-wrap:anywhere}@media(max-width:600px){main{padding:12px}section{padding:12px}}</style></head><body><main>',
             '<h1>AgentEval 离线评测报告</h1><p>仅展示保存结果。执行成功、质量通过、真实来源核验分别解释。</p>',
             '<section><h2>运行与质量结果</h2><dl><dt>运行 / 评分集</dt><dd>' + escape(value["run"]["id"] + " / " + value["score_set"]["id"]) + '</dd>',
             '<dt>通过 / 失败 / 未判定 / 冻结总数</dt><dd>' + escape(f'{summary["P"]} / {summary["F"]} / {summary["U"]} / {summary["N"]}') + '</dd>',
             '<dt>有效通过率 P/(P+F) / 可判定覆盖率 (P+F)/N</dt><dd>' + escape(str(summary["effective_pass_rate"]) + " / " + str(summary["determinate_coverage"])) + '</dd></dl>',
             '<details open><summary>全部统计、来源、版本与冻结条件</summary><pre>' + pretty(value) + '</pre></details></section>',
             '<section><h2>人工复核历史</h2><p>人工结论与原 Judge 分开保存；填报姓名为本机用户声明。</p><pre>' + pretty(document["human_reviews"]) + '</pre></section>']
    for item in document["cases"]:
        case = item["case"]
        parts.append('<section><h2>' + escape(case["case_key"]) + '</h2><dl>')
        for label, text in (("原问题", case["case_snapshot"].get("question")), ("冻结参考行为", case["case_snapshot"].get("expected_answer")), ("Agent 实际回答", case.get("answer"))):
            parts.append('<dt>' + label + '</dt><dd>' + escape("未提供" if text is None else text, quote=True) + '</dd>')
        parts.append('</dl><details><summary>完整单题评分、轨迹、观测、备注和原始证据</summary><pre>' + pretty(item) + '</pre></details></section>')
    parts.append('<section><h2>证据范围</h2><pre>' + pretty(document["limitations"]) + '</pre></section></main></body></html>')
    return "".join(parts)


def export_report(session, run_id, score_set_id=None, format="json"):
    if format not in {"json", "csv", "html"}:
        raise ApiError(422, "INVALID_EXPORT_FORMAT", "导出格式只支持 json、csv、html")
    document = export_document(session, run_id, score_set_id)
    report_data = document["report"]
    filename = f'agenteval-{report_data["run"]["id"]}-{report_data["score_set"]["id"]}.{format}'
    if format == "json":
        content, media = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), "application/json"
    elif format == "csv":
        content, media = _export_csv(document), "text/csv"
    else:
        content, media = _export_html(document), "text/html"
    return {"content": content.encode("utf-8"), "media_type": media, "filename": filename}
