"""校准入口的离线基础设施验证；这些替身结果不属于真实Judge证据。"""

import importlib.util
import json
from pathlib import Path
import sys

from agenteval.evaluation_models import JudgedScore, JudgeAttemptRecord, JudgeResult
from agenteval.judge import applicable_quality_metrics


def load_script():
    source = Path(__file__).resolve().parents[2] / "scripts/calibrate_judge.py"
    spec = importlib.util.spec_from_file_location("calibration_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eight_cases_incremental_save_and_backend_conclusions(tmp_path, monkeypatch):
    module = load_script()
    private, receipt = tmp_path / "private.json", tmp_path / "receipt.json"
    calls = []
    class StubJudge:
        def __init__(self, profile):
            pass

        def evaluate(self, case, execution, *, deadline, on_attempt):
            calls.append(case["case_key"])
            # 同一profile执行，调用前私有文件已含本题冻结输入。
            assert json.loads(private.read_text("utf-8"))["cases"][-1]["case_key"] == case["case_key"]
            scores = {name: JudgedScore(score=5, reason="协议替身") for name in applicable_quality_metrics(execution)}
            record = JudgeAttemptRecord(attempt=1, status="SUCCEEDED", response={"parsed": {name: score.model_dump() for name, score in scores.items()}})
            on_attempt(record)
            saved = json.loads(private.read_text("utf-8"))
            assert saved["calls"] == len(calls) and saved["cases"][-1]["judge"]["attempts"]
            return JudgeResult(status="SUCCEEDED", scores=scores, attempts=[record])
    monkeypatch.setattr(module, "JudgeClient", StubJudge)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-test-private-canary")
    monkeypatch.setattr(sys, "argv", ["calibrate_judge.py", "--allow-real", "--private-output", str(private), "--receipt", str(receipt)])
    assert module.main() == 0
    saved = json.loads(receipt.read_text("utf-8"))
    assert len(calls) == 8 and saved["valid_cases"] == 8
    assert saved["status"] == "REVIEW_REQUIRED" and saved["matched_cases"] == 4
    assert all(row["assessment"]["conclusion"] == "PASS" for row in saved["cases"])
    assert all(row["review"]["human_reviewed"] is False for row in saved["cases"])
    assert "offline-test-private-canary" not in receipt.read_text("utf-8")


def test_call_budget_stops_at_eight_even_when_all_attempts_fail(tmp_path, monkeypatch):
    module = load_script()
    private, receipt = tmp_path / "private.json", tmp_path / "receipt.json"
    calls = []
    class StubJudge:
        def __init__(self, profile):
            pass

        def evaluate(self, case, execution, *, deadline, on_attempt):
            attempts = []
            for number in (1, 2):
                calls.append((case["case_key"], number))
                record = JudgeAttemptRecord(attempt=number, status="ERROR", error={"code": "STUB_FAILURE"})
                attempts.append(record)
                on_attempt(record)
            return JudgeResult(status="ERROR", attempts=attempts, error_code="STUB_FAILURE")
    monkeypatch.setattr(module, "JudgeClient", StubJudge)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-key")
    monkeypatch.setattr(sys, "argv", ["calibrate_judge.py", "--allow-real", "--max-calls", "8", "--private-output", str(private), "--receipt", str(receipt)])
    assert module.main() == 1
    saved = json.loads(receipt.read_text("utf-8"))
    assert len(calls) == saved["calls"] == 8
    assert saved["valid_cases"] == 0 and len(saved["cases"]) == 8
    assert all(row["assessment"]["conclusion"] == "UNDETERMINED" for row in saved["cases"])
    assert sum(len(row["judge"]["attempts"]) for row in saved["cases"]) == 8
