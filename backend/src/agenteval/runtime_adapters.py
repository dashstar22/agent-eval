"""从冻结配置建立适配器，并在每次新外部请求前执行锁与预算守卫。"""

import time
from urllib.parse import urlsplit

from .adapters.http import HttpAdapter
from .adapters.mock import MockAdapter
from .adapters.printer import PrinterHttpAdapter
from .config import ConnectionConfig
from .judge import JudgeClient
from .schemas import JudgeProfile


class RuntimeOwnershipLostGuard(RuntimeError):
    """不能被适配器的协议错误处理吞掉。"""


class RuntimeBudgetExceeded(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class RequestGuard:
    def __init__(self, ensure_owner, budget, *, deadline=None):
        self.ensure_owner = ensure_owner
        self.budget = budget
        self.deadline = deadline
        self.case_deadline = None
        self.agent_calls = 0
        self.total_calls = 0

    def check(self):
        try:
            self.ensure_owner()
        except Exception as exc:
            raise RuntimeOwnershipLostGuard("RUNTIME_OWNERSHIP_LOST") from exc
        current = time.monotonic()
        if self.deadline is not None and current >= self.deadline:
            raise RuntimeBudgetExceeded("RUN_DEADLINE_EXCEEDED")
        if self.case_deadline is not None and current >= self.case_deadline:
            raise RuntimeBudgetExceeded("CASE_DEADLINE_EXCEEDED")

    def external(self, kind):
        self.check()
        if kind not in {"AGENT", "JUDGE"}:
            return
        maximum = self.budget.get("max_total_calls")
        if maximum is not None and self.total_calls >= maximum:
            raise RuntimeBudgetExceeded("RUN_CALL_BUDGET_EXCEEDED")
        if kind == "AGENT":
            if self.agent_calls >= self.budget["max_agent_calls"]:
                raise RuntimeBudgetExceeded("AGENT_CALL_BUDGET_EXCEEDED")
            self.agent_calls += 1
        self.total_calls += 1

    def agent_request(self, adapter_type, method, path):
        model_turn = method.upper() == "POST" and (
            adapter_type == "HTTP" or
            (adapter_type == "PRINTER_HTTP" and urlsplit(path).path.endswith("/messages")))
        self.external("AGENT" if model_turn else "CONTROL")


def build_adapter(snapshot, budget, guard, *, transport=None, environment=None):
    agent = snapshot["agent"]
    if agent["adapter_type"] == "MOCK":
        return MockAdapter()
    remaining = min(float(budget["case_timeout_seconds"]),
                    max(0.001, guard.case_deadline - time.monotonic()))
    refs = {key: value for key, value in agent.get("credential_refs", {}).items() if value}
    config = ConnectionConfig(
        base_url=agent["base_url"], source_type=agent["source_type"],
        request_timeout_seconds=min(float(agent["timeout_ms"]) / 1000, remaining, 360),
        case_timeout_seconds=remaining,
        observation_wait_seconds=budget["observation_wait_seconds"],
        response_max_bytes=budget["response_max_bytes"],
        max_agent_calls=budget["max_agent_calls"], **refs,
    )
    cls = HttpAdapter if agent["adapter_type"] == "HTTP" else PrinterHttpAdapter
    return cls(config, transport=transport, environment=environment,
               before_request=lambda method, path: guard.agent_request(agent["adapter_type"], method, path))


def build_judge(snapshot, budget, guard, *, transport=None, environment=None):
    # 来源标记由调用配置决定；网络替身测试显式注入 PROTOCOL_DOUBLE。
    profile = JudgeProfile.model_validate(snapshot["judge"])
    return JudgeClient(
        profile, transport=transport, environment=environment,
        response_max_bytes=budget["response_max_bytes"],
        case_timeout_seconds=budget["case_timeout_seconds"],
        source_type=getattr(profile, "source_type", "REAL_EXTERNAL"),
        before_request=lambda: guard.external("JUDGE"),
    )
