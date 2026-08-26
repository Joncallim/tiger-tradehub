from __future__ import annotations

import re
from typing import Any

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
SENSITIVE_KEYS = {"token", "private_key", "tiger_id", "tiger_account", "authorization"}


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered in SENSITIVE_KEYS
        or lowered.endswith("_token")
        or lowered.endswith("_private_key")
        or "authorization" in lowered
        or lowered.startswith("tigeropen_")
    )


def sanitize(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        if key and _is_sensitive_key(key):
            return "[REDACTED]"
        redacted = PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", value)
        if redacted != value:
            return redacted
        # value-based: registered secret values (config tokens, raw keys)
        return _redact_registered_secrets(redacted)
    if isinstance(value, dict):
        return {name: sanitize(item, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def _redact_registered_secrets(value: str) -> str:
    """Redact configured secret values appearing anywhere in the text.

    Registered from the research settings (tiingo/api tokens).  Deliberately
    NOT shape-based: acceptance output is full of legitimate 64-char hex
    identifiers (run/decision/proposal hashes) and 40-char commit SHAs, so a
    token-shape heuristic would destroy the output's integrity.
    """
    from tradehub_research.config import ResearchSettings

    try:
        settings = ResearchSettings()
    except Exception:
        settings = None
    if settings is None:
        return value
    for field_name in ("tiingo_token", "api_token"):
        secret = getattr(settings, field_name, None)
        if isinstance(secret, str) and secret and secret in value:
            value = value.replace(secret, "[REDACTED]")
    return value
