"""请求截止与响应体上限由所有 HTTP 连接共用。"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from ..config import ConnectionConfig
from ..models import ExecutionResult, now

SaveAnswer = Callable[[ExecutionResult], None]


class ProtocolFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class HttpSession:
    """同步适配器持有独立事件循环，以绝对截止取消慢速响应读取。"""

    def __init__(self, *, base_url: str = "", transport=None, before_request=None):
        self.loop = asyncio.new_event_loop()
        self.before_request = before_request
        self.client = httpx.AsyncClient(base_url=base_url, transport=transport, follow_redirects=False, trust_env=False)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            self.loop.run_until_complete(self.client.aclose())
        finally:
            self.loop.close()

    def request(self, budget: "Budget", method: str, path: str, **kwargs):
        return self.loop.run_until_complete(self._request(budget, method, path, **kwargs))

    async def _request(self, budget: "Budget", method: str, path: str, **kwargs):
        # RuntimeError 守卫须穿透协议错误处理，避免丢锁后继续观测或重放。
        if self.before_request is not None:
            self.before_request(method, path)
        timeout = min(budget.config.request_timeout_seconds, budget.remaining())
        try:
            async with asyncio.timeout(timeout):
                async with self.client.stream(method, path, timeout=timeout, **kwargs) as response:
                    body = bytearray()
                    async for block in response.aiter_bytes():
                        budget.remaining()
                        body.extend(block)
                        if len(body) > budget.config.response_max_bytes:
                            raise ProtocolFailure("RESPONSE_TOO_LARGE")
                    try:
                        parsed = json.loads(body)
                    except (ValueError, UnicodeDecodeError):
                        raise ProtocolFailure("INVALID_JSON") from None
                    if not isinstance(parsed, dict):
                        raise ProtocolFailure("INVALID_RESPONSE_OBJECT")
                    return parsed, response.headers, response.status_code
        except TimeoutError:
            raise httpx.ReadTimeout("ABSOLUTE_REQUEST_DEADLINE") from None


@dataclass
class Budget:
    config: ConnectionConfig
    started: float

    def remaining(self) -> float:
        value = self.config.case_timeout_seconds - (time.monotonic() - self.started)
        if value <= 0:
            raise ProtocolFailure("CASE_DEADLINE_EXCEEDED")
        return value


def request_json(client: HttpSession, budget: Budget, method: str, path: str, **kwargs) -> tuple[dict[str, Any], httpx.Headers, int]:
    return client.request(budget, method, path, **kwargs)


def result_error(config: ConnectionConfig, started_at: str, started: float, code: str, status: str = "FAILED", **facts) -> ExecutionResult:
    return ExecutionResult(
        started_at=started_at, ended_at=now(), duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        agent_status=status, source_type=config.source_type, error_code=code, **facts,
    )
