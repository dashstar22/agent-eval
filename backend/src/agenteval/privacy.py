"""写入前和读取时双重脱敏；原始证据有界且明确截断。"""

import json
import os
import re
from typing import Any, Mapping

from .safety import COUNTERS, EMAIL, PHONE, SENSITIVE

AUTH_LINE = re.compile(r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie|x-csrf-token|x-xsrf-token|x-api-key)\s*[:=]\s*[^\r\n]+")
ASSIGNMENT = re.compile(r"(?i)\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token|csrf[_ -]?token)\s*[:=]\s*[^\s,;]+")
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")
SAFE_COUNTER_FIELDS = {
    "known_input_tokens_records", "known_output_tokens_records", "known_total_tokens_records",
    "unit_tokens", "max_tokens", "max_completion_tokens",
}


def known_secrets(refs: Mapping[str, str | None] | None = None) -> tuple[str, ...]:
    names = {name for name in os.environ if re.search(r"PASSWORD|SECRET|TOKEN|API_KEY", name, re.I)}
    names.update(v for v in (refs or {}).values() if v)
    return tuple(sorted({os.environ[n] for n in names if os.environ.get(n)}, key=len, reverse=True))


def sanitize(value: Any, secrets: tuple[str, ...] = (), depth: int = 0) -> Any:
    if depth > 40:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, dict):
        # 外部响应的动态键也可能包含凭证或个人信息，不能只过滤值。
        return {sanitize(str(k), secrets, depth + 1): "[REDACTED]" if (SENSITIVE.search(str(k)) or re.search(r"api[_-]?key", str(k), re.I)) and re.sub(r"[_-]", "", str(k).lower()) not in COUNTERS and str(k) not in SAFE_COUNTER_FIELDS else sanitize(v, secrets, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, secrets, depth + 1) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = PHONE.sub("[REDACTED_PHONE]", EMAIL.sub("[REDACTED_EMAIL]", value))
        value = URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
        return BEARER.sub("Bearer [REDACTED]", ASSIGNMENT.sub("[REDACTED_CREDENTIAL]", AUTH_LINE.sub("[REDACTED_AUTH_HEADER]", value)))
    return value


def bounded_raw(value: Any, secrets: tuple[str, ...] = (), max_bytes: int = 2097152) -> dict:
    safe = sanitize(value, secrets)
    encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > max_bytes:
        return {"data": encoded[:max_bytes].decode("utf-8", errors="ignore"), "completeness": "PARTIAL", "reason": "SIZE_LIMIT", "original_bytes": len(encoded)}
    return {"data": safe, "completeness": "COMPLETE", "reason": None}
