"""确定性开发替身，来源标记不可被输出内容覆盖。"""

from ..models import ExecutionResult, Usage, now
from .base import SaveAnswer


class MockAdapter:
    def run(self, question: str, save_answer: SaveAnswer | None = None, **_) -> ExecutionResult:
        stamp = now()
        result = ExecutionResult(
            answer=f"[MOCK] {question}", business_status="MOCK_RESPONSE", agent_status="SUCCEEDED",
            started_at=stamp, ended_at=stamp, duration_ms=0, source_type="MOCK",
            tools=[], tools_completeness="COMPLETE", contexts=[], context_scope="NOT_USED",
            context_completeness="COMPLETE", usage=Usage(status="NOT_REQUESTED", reason="MOCK_NO_MODEL"),
            observation_status="COMPLETE", observation_reason="MOCK", raw_agent={"source_type": "MOCK"},
        )
        if save_answer:
            save_answer(result)
        return result
