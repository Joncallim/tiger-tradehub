"""Secret sanitisation for acceptance results and artifacts.

The runner — never the agent — owns sanitisation. Every string that will
appear in a public result or artifact is passed through `sanitize_text`
or `sanitize_value` before serialisation.

Known sensitive values come from the deployment settings (API token,
Tiger ID, Tiger account, private key, Telegram token) plus generic
pattern redaction for confirmation-token-shaped strings.
"""

from __future__ import annotations

import re
from typing import Any

from tradehub.config import Settings, secret_value

# Confirmation tokens are produced by secrets.token_urlsafe(24): 32
# characters drawn from [A-Za-z0-9_-]. Redact any standalone string of
# that shape so a leaked token cannot survive into artifacts.
TOKEN_URLSAFE_RE = re.compile(r"[A-Za-z0-9_-]{32}")

# PEM private key blocks (both PKCS#1 and PKCS#8 headers).
PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

SENSITIVE_KEYS = {
    "confirmation_token",
    "api_token",
    "private_key",
    "telegram_bot_token",
    "authorization",
    "token",
}


class Sanitizer:
    """Redacts configured secrets plus known-sensitive patterns."""

    def __init__(self, settings: Settings | None = None):
        self.values: set[str] = set()
        if settings is not None:
            self.register(settings.api_token)
            self.register(settings.tiger_id)
            self.register(settings.tiger_account)
            self.register(settings.tiger_private_key)
            self.register(settings.telegram_bot_token)

    def register(self, value: Any) -> None:
        if value is None:
            return
        if hasattr(value, "get_secret_value"):  # SecretStr
            raw = value.get_secret_value()
        else:
            raw = str(value)
        if raw and len(raw) >= 4:
            self.values.add(raw)

    def sanitize_text(self, text: str) -> str:
        redacted = PEM_PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
        for value in sorted(self.values, key=len, reverse=True):
            redacted = redacted.replace(value, "[REDACTED]")
        return redacted

    def sanitize_value(self, value: Any, key: str | None = None) -> Any:
        """Recursively sanitize a JSON-able structure.

        Values under sensitive keys are always redacted in full; other
        strings get value-level replacement plus token-shape redaction.
        """
        if isinstance(value, str):
            if key is not None and key.lower() in SENSITIVE_KEYS:
                return "[REDACTED]"
            text = TOKEN_URLSAFE_RE.sub("[REDACTED TOKEN]", value)
            return self.sanitize_text(text)
        if isinstance(value, dict):
            return {k: self.sanitize_value(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.sanitize_value(item, key) for item in value]
        return value


def build_sanitizer(settings: Settings | None = None) -> Sanitizer:
    sanitizer = Sanitizer(settings)
    # Inline private key from the environment as a fallback.
    if settings is not None:
        inline = secret_value(settings.tiger_private_key)
        if inline:
            for chunk in (inline.replace("\\n", "\n"), inline):
                sanitizer.register(chunk)
    return sanitizer
