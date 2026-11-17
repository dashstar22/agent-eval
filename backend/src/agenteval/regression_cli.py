"""已保存评测结果的回归 CLI：0通过、1失败、2无法判断、3请求/输入错误。"""

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .analysis_schemas import AnalysisSelection, RegressionCheckInput, RegressionPolicy
from .privacy import known_secrets, sanitize


class InputError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError("命令参数不合法；请使用 --help 查看格式")


def _markdown(value):
    def cell(item):
        return str(item if item is not None else "不可用").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    lines = ["# AgentEval 回归检查", "", f'结论：{cell(value["verdict"])}', "", "仅分析保存结果，不请求 Agent 或 Judge。", "",
             "| 检查 | 结果 | 实际值 | 阈值 | 原因 |", "| --- | --- | --- | --- | --- |"]
    for row in value.get("checks", []):
        lines.append("| " + " | ".join(cell(row.get(key)) for key in ("label", "status", "actual", "threshold", "reason")) + " |")
    lines.extend(["", f'新增失败：{len(value.get("new_failures", []))}；修复：{len(value.get("fixed_cases", []))}；未知：{len(value.get("unknown_cases", []))}。', "", "## 条件与证据", "", "```json", json.dumps(value.get("conditions", {}), ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="API 根地址，例如 http://127.0.0.1:18080")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--baseline-score")
    parser.add_argument("--candidate-score")
    parser.add_argument("--max-new-failures", type=int, default=0)
    parser.add_argument("--min-coverage", type=float, default=1.0)
    parser.add_argument("--max-pass-rate-drop", type=float, default=0.0)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    try:
        args = parser.parse_args(argv)
        parsed = urlsplit(args.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise InputError("API 地址须为 HTTP(S)，不允许嵌入凭据、查询参数或片段")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise InputError("API 端口须为 1..65535")
        payload = RegressionCheckInput(baseline=AnalysisSelection(run_id=args.baseline, score_set_id=args.baseline_score),
                                       candidate=AnalysisSelection(run_id=args.candidate, score_set_id=args.candidate_score),
                                       policy=RegressionPolicy(max_new_failures=args.max_new_failures, min_coverage=args.min_coverage,
                                                               max_pass_rate_drop=args.max_pass_rate_drop))
        base = args.base_url.rstrip("/")
        if not base.endswith("/api/v1"):
            base += "/api/v1"
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
            response = client.post(base + "/extensions/regression-checks", json=payload.model_dump(exclude_none=True))
        if response.status_code != 200:
            raise InputError("回归检查请求失败，HTTP " + str(response.status_code))
        value = sanitize(response.json(), known_secrets())
        if not isinstance(value, dict) or value.get("verdict") not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise InputError("后端返回无效的回归检查结果")
        content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) if args.format == "json" else _markdown(value)
        if args.output:
            # CI 输出必须显式选择新路径，不覆盖已有报告或源文件。
            with args.output.open("x", encoding="utf-8", newline="") as handle:
                handle.write(content + "\n")
        else:
            print(content)
        return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[value["verdict"]]
    except (InputError, ValidationError, httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, TypeError) as exc:
        message = str(exc) if isinstance(exc, InputError) else "请求、输入或输出文件处理失败；未产生新的模型调用"
        print(json.dumps({"error": sanitize(message, known_secrets()), "exit_code": 3}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
