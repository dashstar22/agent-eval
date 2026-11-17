"""原始输出的保守脱敏；不保留 HTTP 认证头或 Cookie。"""

import re
from typing import Any

SENSITIVE = re.compile(r"password|passwd|secret|token|cookie|authorization|csrf|email|phone|contact|address|serial", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
COUNTERS = {"inputtokens", "outputtokens", "totaltokens", "observedinputtokens", "observedoutputtokens", "observedtotaltokens"}


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if SENSITIVE.search(str(k)) and re.sub(r"[_-]", "", str(k).lower()) not in COUNTERS else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return PHONE.sub("[REDACTED_PHONE]", EMAIL.sub("[REDACTED_EMAIL]", value))
    return value
