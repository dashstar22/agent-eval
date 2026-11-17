"""单题隔离实例探针；先原子保存回答，再有界导入观测。"""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from .adapters import PrinterHttpAdapter
from .config import ConnectionConfig
from .models import ExecutionResult


def atomic_save(path: Path, result: ExecutionResult):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description="仅向显式指定的隔离实例执行一个知识问题")
    parser.add_argument("--question", required=True)
    parser.add_argument("--order-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("输出文件已存在，请选用新的证据文件名")
    if os.environ.get("AGENTEVAL_ALLOW_REAL") != "true":
        parser.error("需要 AGENTEVAL_ALLOW_REAL=true 和明确的实例、总时长与调用次数预算")
    for name in ("AGENTEVAL_PRINTER_BASE_URL", "AGENTEVAL_MAX_AGENT_CALLS", "AGENTEVAL_CASE_TIMEOUT_SECONDS"):
        if not os.environ.get(name):
            parser.error(f"缺少配置 {name}")
    config = ConnectionConfig(
        base_url=os.environ["AGENTEVAL_PRINTER_BASE_URL"], max_agent_calls=int(os.environ["AGENTEVAL_MAX_AGENT_CALLS"]),
        case_timeout_seconds=float(os.environ["AGENTEVAL_CASE_TIMEOUT_SECONDS"]),
        request_timeout_seconds=float(os.environ.get("AGENTEVAL_REQUEST_TIMEOUT_SECONDS", "195")),
        observation_wait_seconds=float(os.environ.get("AGENTEVAL_OBSERVATION_WAIT_SECONDS", "10")),
        username_ref=os.environ.get("AGENTEVAL_PRINTER_USERNAME_REF", "PRINTER_USERNAME"),
        password_ref=os.environ.get("AGENTEVAL_PRINTER_PASSWORD_REF", "PRINTER_PASSWORD"),
    )
    result = PrinterHttpAdapter(config).run(arguments.question, lambda answer: atomic_save(arguments.output, answer), order_id=arguments.order_id)
    atomic_save(arguments.output, result)
    # Safe receipt: neither login identity nor authentication payload appears in stdout.
    print(json.dumps({"agent_status": result.agent_status, "business_status": result.business_status, "trace_ref": result.trace_ref,
        "observation_status": result.observation_status, "observation_reason": result.observation_reason,
        "source_type": result.source_type, "model_execution_verified": result.model_execution_verified,
        "usage_status": result.usage.status, "output": str(arguments.output.resolve())}, ensure_ascii=False))
    return 0 if result.agent_status == "SUCCEEDED" and result.model_execution_verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
