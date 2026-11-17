"""八条合成候选回答的真实 Judge 校准；不调用 Agent，不覆盖旧证据。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from agenteval.evaluation_models import JudgeResult
from agenteval.judge import JudgeClient
from agenteval.models import Context, ExecutionResult, now
from agenteval.privacy import known_secrets, sanitize
from agenteval.rules import combine_metrics, evaluate_rules
from agenteval.schemas import CaseCreate, DEFAULT_REQUIRED, JudgeProfile, RuleProfile

ROOT = Path(__file__).resolve().parents[1]


def write_new_state(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), "utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real", action="store_true")
    parser.add_argument("--base-url", default=os.environ.get("AGENTEVAL_JUDGE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
    parser.add_argument("--model", default=os.environ.get("AGENTEVAL_JUDGE_MODEL", "qwen-plus"))
    parser.add_argument("--credential-ref", default=os.environ.get("AGENTEVAL_JUDGE_CREDENTIAL_REF", "DASHSCOPE_API_KEY"))
    parser.add_argument("--samples", type=Path, default=ROOT / "data/judge-calibration/cases.json")
    parser.add_argument("--private-output", type=Path, default=ROOT / ".local-validation/group3-calibration.json")
    parser.add_argument("--receipt", type=Path, default=ROOT / "docs/evidence/g3-judge-calibration-20261116.json")
    parser.add_argument("--max-calls", type=int, default=16)
    parser.add_argument("--total-timeout", type=int, default=600)
    args = parser.parse_args()
    if not args.allow_real:
        parser.error("真实Judge需显式--allow-real；此入口不执行Agent")
    if not 8 <= args.max_calls <= 16 or not 1 <= args.total_timeout <= 600:
        parser.error("本轮预算最多16次Judge调用、600秒")
    if args.private_output.exists() or args.receipt.exists():
        parser.error("输出已存在，拒绝覆盖真实证据；请指定新路径")
    if not os.environ.get(args.credential_ref):
        parser.error("Judge凭证引用未配置")
    fixtures = json.loads(args.samples.read_text("utf-8"))
    samples = fixtures["samples"]
    if len(samples) != 8:
        parser.error("本次校准固定八条样例")
    profile = JudgeProfile(base_url=args.base_url, model=args.model, credential_ref=args.credential_ref)
    rules = RuleProfile(quality_thresholds={"correctness": 4, "relevance": 4, "task_completion": 4, "faithfulness": 4})
    client = JudgeClient(profile)
    started, call_count = time.monotonic(), 0
    state = {
        "schema_version": "agenteval-judge-calibration-result-v1", "created_at": now(),
        "scope": "八条AI编写合成候选回答；真实外部Judge；0次Agent调用；非真实Agent质量或独立人工证据",
        "judge_source_type": "REAL_EXTERNAL", "candidate_source_type": "MOCK",
        "candidate_source": fixtures["candidate_source"], "human_reviewed": False,
        "reviewer_type": "AI_REVIEW", "same_vendor_model_bias":
            "Judge与既有被测Agent同为DashScope qwen-plus，存在同源偏差"
            if args.model == "qwen-plus" and args.base_url.startswith("https://dashscope.aliyuncs.com/")
            else "Judge实际配置见profile，未核实与被测Agent模型的独立性，不能宣称独立人工质量证据",
        "profile": profile.model_dump(), "rule_profile": rules.model_dump(),
        "fixture_sha256": hashlib.sha256(args.samples.read_bytes()).hexdigest(),
        "budget": {"max_calls": args.max_calls, "single_timeout_seconds": 60, "total_timeout_seconds": args.total_timeout,
                   "response_max_bytes": 2097152, "agent_calls": 0},
        "calls": 0, "status": "RUNNING", "cases": [],
    }
    secrets = known_secrets({"judge": args.credential_ref})
    write_new_state(args.private_output, sanitize(state, secrets))
    for sample in samples:
        required = DEFAULT_REQUIRED + (["faithfulness"] if sample.get("required_faithfulness") else [])
        case = CaseCreate(case_key=sample["case_key"], question=sample["question"], expected_answer=sample["expected_answer"],
                          required_metrics=required, source={"type": fixtures["candidate_source"], "category": sample["category"]},
                          tags=["synthetic-judge-calibration"], license=fixtures["license"]).model_dump()
        contexts = [Context(text=text, source="AI_AUTHORED_SYNTHETIC_EVIDENCE", version="calibration-v1")
                    for text in sample.get("contexts", [])]
        execution = ExecutionResult(answer=sample["candidate_answer"], agent_status="SUCCEEDED", started_at=now(), ended_at=now(),
                                    duration_ms=0, source_type="MOCK", tools=[], tools_completeness="COMPLETE",
                                    contexts=contexts or None, context_scope="ACTUAL_MODEL_CONTEXT" if contexts else "NOT_USED",
                                    context_completeness="COMPLETE" if contexts else "UNAVAILABLE",
                                    raw_agent={"synthetic_candidate": True, "no_agent_call": True})
        row = {"case_key": sample["case_key"], "category": sample["category"], "case_snapshot": case,
               "execution": execution.model_dump(), "expected_conclusion": sample["expected_conclusion"],
               "expectation_reason": sample["expectation_reason"], "human_reviewed": False, "reviewer_type": "AI_REVIEW",
               "judge": JudgeResult(status="NA").model_dump()}
        state["cases"].append(row)
        write_new_state(args.private_output, sanitize(state, secrets))
        def save_attempt(record):
            nonlocal call_count
            call_count += 1
            row["judge"]["attempts"].append(record.model_dump())
            state["calls"] = call_count
            # 每次真实调用先独立写盘，再继续重试或下一样例。
            write_new_state(args.private_output, sanitize(state, secrets))
            if call_count >= args.max_calls and record.status == "ERROR":
                raise RuntimeError("CALIBRATION_CALL_BUDGET_EXHAUSTED")
        if call_count >= args.max_calls or time.monotonic() >= started + args.total_timeout:
            result = JudgeResult(status="ERROR", error_code="CALIBRATION_BUDGET_EXHAUSTED")
        else:
            try:
                result = client.evaluate(case, execution, deadline=started + args.total_timeout, on_attempt=save_attempt)
            except RuntimeError as error:
                if str(error) != "CALIBRATION_CALL_BUDGET_EXHAUSTED":
                    raise
                result = JudgeResult(status="ERROR", attempts=row["judge"]["attempts"], error_code=str(error))
        row["judge"] = result.model_dump()
        assessment = combine_metrics(case, rules, evaluate_rules(case, execution, rules), result)
        row["assessment"] = assessment.model_dump()
        actual = assessment.conclusion
        row["actual_conclusion"] = actual
        row["matches_expectation"] = actual == sample["expected_conclusion"]
        row["review"] = {
            "reviewer_type": "AI_REVIEW", "human_reviewed": False,
            "disputed": not row["matches_expectation"],
            "reason": "与事先记录的明确预期一致；仅AI复核，未证明真实Agent质量" if row["matches_expectation"]
                      else "实际Judge结论与预设预期不同，保留分数与原文，不修改样例或阈值；需要后续人工复核",
        }
        write_new_state(args.private_output, sanitize(state, secrets))
        print(json.dumps({"case_key": row["case_key"], "judge_status": result.status, "conclusion": actual,
                          "matches_expectation": row["matches_expectation"], "calls": call_count}, ensure_ascii=False), flush=True)
    state["completed_at"] = now()
    state["duration_ms"] = round((time.monotonic() - started) * 1000)
    state["valid_cases"] = sum(row["judge"]["status"] == "SUCCEEDED" for row in state["cases"])
    state["matched_cases"] = sum(row["matches_expectation"] for row in state["cases"])
    state["status"] = "PASS" if state["valid_cases"] == 8 and state["matched_cases"] == 8 else "REVIEW_REQUIRED"
    state["limitations"] = ["阈值4是本项目建议值，八个已知预期不能证明行业标准", "候选回答为AI合成而非本次Agent执行",
                            "复核者是AI且未人工复核，非独立人工质量证据", "同源模型偏差及模型别名不可变快照未核实",
                            "未配置可核验价格，cost=null；仅按返回usage保留真实可用Token"]
    safe = sanitize(state, secrets)
    write_new_state(args.private_output, safe)
    receipt = safe | {"private_result_sha256": hashlib.sha256(args.private_output.read_bytes()).hexdigest()}
    write_new_state(args.receipt, receipt)
    print(json.dumps({"status": state["status"], "valid_cases": state["valid_cases"], "matched_cases": state["matched_cases"],
                      "calls": call_count, "receipt": str(args.receipt.resolve())}, ensure_ascii=False))
    return 0 if state["valid_cases"] == 8 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError):
        print("Judge校准未完成；已落盘尝试保留，检查私有结果和配置，不自动重跑", file=sys.stderr)
        raise SystemExit(2)
