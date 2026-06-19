import pytest
from pydantic import ValidationError

from tradehub.config import Settings

STRONG_TOKEN = "test-token-with-enough-length"


def test_settings_masks_secrets_in_repr_and_dump():
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TIGEROPEN_PRIVATE_KEY="private-key-value",
        TELEGRAM_BOT_TOKEN="telegram-token-value",
    )
    rendered = f"{settings!r} {settings.model_dump()}"

    assert STRONG_TOKEN not in rendered
    assert "private-key-value" not in rendered
    assert "telegram-token-value" not in rendered
    assert "**********" in rendered


@pytest.mark.parametrize("token", ["", "change-me", "short"])
def test_api_token_must_be_strong(token):
    with pytest.raises(ValidationError, match="TRADEHUB_API_TOKEN"):
        Settings(TRADEHUB_API_TOKEN=token)
