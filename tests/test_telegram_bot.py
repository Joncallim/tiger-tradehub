import asyncio
import sys
import types

from tradehub import telegram_bot
from tradehub.config import Settings


def test_reconcile_command_posts_reconcile_and_formats_status(monkeypatch):
    calls = []

    class FakeClient:
        async def post(self, path: str, payload: dict[str, object]):
            calls.append((path, payload))
            return {"status": "resolved", "order_id": "broker-123"}

    class FakeChat:
        def __init__(self, chat_id: int) -> None:
            self.id = chat_id

    class FakeMessage:
        def __init__(self) -> None:
            self.texts = []

        async def reply_text(self, text: str, parse_mode: str | None = None):
            self.texts.append((text, parse_mode))

    class FakeUpdate:
        def __init__(self, chat_id: int) -> None:
            self.effective_chat = FakeChat(chat_id)
            self.message = FakeMessage()

    class FakeContext:
        def __init__(self, args: list[str] | None = None) -> None:
            self.args = args or []

    class FakeApplication:
        def __init__(self) -> None:
            self.handlers = []
            self.ran = False

        def add_handler(self, handler) -> None:
            self.handlers.append(handler)

        def run_polling(self) -> None:
            self.ran = True

    class FakeApplicationBuilder:
        def token(self, _token: str):
            return self

        def build(self):
            return fake_application

    class FakeCommandHandler:
        def __init__(self, command: str, callback) -> None:
            self.command = command
            self.callback = callback

    fake_application = FakeApplication()

    fake_contexttypes = types.SimpleNamespace(DEFAULT_TYPE=None)
    fake_ext_module = types.ModuleType("telegram.ext")
    fake_ext_module.Application = types.SimpleNamespace(builder=lambda: FakeApplicationBuilder())
    fake_ext_module.CommandHandler = FakeCommandHandler
    fake_ext_module.ContextTypes = fake_contexttypes

    fake_telegram_module = types.ModuleType("telegram")
    fake_telegram_module.Update = FakeUpdate

    monkeypatch.setitem(sys.modules, "telegram", fake_telegram_module)
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext_module)

    monkeypatch.setattr(telegram_bot, "TradeHubClient", lambda: FakeClient())
    monkeypatch.setattr(
        telegram_bot,
        "get_settings",
        lambda: Settings(
            TRADEHUB_API_TOKEN="test-token-with-enough-length",
            TELEGRAM_BOT_TOKEN="telegram-token",
            TELEGRAM_ALLOWED_CHAT_IDS="1",
        ),
    )

    telegram_bot.main()

    reconcile_handler = next(
        handler for handler in fake_application.handlers if handler.command == "reconcile"
    )
    update = FakeUpdate(1)
    context = FakeContext(["token-1"])
    asyncio.run(reconcile_handler.callback(update, context))

    assert fake_application.ran is True
    assert calls == [("/orders/submit/reconcile", {"confirmation_token": "token-1"})]
    assert any("reconcile" in text.lower() for text, _ in update.message.texts)
