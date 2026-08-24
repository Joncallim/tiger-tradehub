from __future__ import annotations

import re
from typing import Any

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
SENSITIVE_KEYS = {"token", "private_key", "tiger_id", "tiger_account", "authorization"}


def sanitize(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        return (
            "[REDACTED]"
            if key and key.lower() in SENSITIVE_KEYS
            else PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", value)
        )
    if isinstance(value, dict):
        return {name: sanitize(item, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value
