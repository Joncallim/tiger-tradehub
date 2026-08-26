from __future__ import annotations

import html

from tradehub.client import TradeHubClient
from tradehub.config import get_settings


def main() -> None:
    try:
        from telegram import Update
        from telegram.ext import Application, CommandHandler, ContextTypes
    except ImportError as exc:
        raise SystemExit("Install Telegram support with: pip install -e '.[telegram]'") from exc

    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not settings.telegram_allowed_chat_ids:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_IDS must contain at least one chat id")

    client = TradeHubClient()
    preview_client = TradeHubClient(preview_only=True)

    def allowed(update: Update) -> bool:
        chat_id = update.effective_chat.id if update.effective_chat else None
        return bool(chat_id and chat_id in settings.telegram_allowed_chat_ids)

    async def reject_if_needed(update: Update) -> bool:
        if allowed(update):
            return False
        if update.message:
            await update.message.reply_text("This chat is not authorized.")
        return True

    async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_needed(update):
            return
        response = await client.get("/health")
        await update.message.reply_text(format_response(response), parse_mode="HTML")

    async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_needed(update):
            return
        try:
            payload = parse_preview_args(context.args)
            response = await preview_client.post("/orders/preview", payload)
        except Exception as exc:
            await update.message.reply_text(f"Preview failed: {exc}")
            return
        await update.message.reply_text(format_response(response), parse_mode="HTML")

    async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await side_shortcut(update, context, "BUY")

    async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await side_shortcut(update, context, "SELL")

    async def side_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE, side: str) -> None:
        if await reject_if_needed(update):
            return
        try:
            if len(context.args) not in (3, 4):
                raise ValueError(f"Usage: /{side.lower()} SYMBOL QTY LIMIT_PRICE [CURRENCY]")
            symbol, quantity, limit_price, *rest = context.args
            payload = {
                "symbol": symbol,
                "side": side,
                "quantity": float(quantity),
                "order_type": "LIMIT",
                "limit_price": float(limit_price),
                "currency": rest[0].upper() if rest else "USD",
            }
            response = await preview_client.post("/orders/preview", payload)
        except Exception as exc:
            await update.message.reply_text(f"Preview failed: {exc}")
            return
        await update.message.reply_text(format_response(response), parse_mode="HTML")

    async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_needed(update):
            return
        try:
            if len(context.args) != 1:
                raise ValueError("Usage: /confirm TOKEN")
            response = await client.post("/orders/submit", {"confirmation_token": context.args[0]})
        except Exception as exc:
            await update.message.reply_text(f"Submit failed: {exc}")
            return
        await update.message.reply_text(format_response(response), parse_mode="HTML")

    async def reconcile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_needed(update):
            return
        try:
            if len(context.args) != 1:
                raise ValueError("Usage: /reconcile TOKEN")
            response = await client.post(
                "/orders/submit/reconcile",
                {"confirmation_token": context.args[0]},
            )
            status = response.get("status")
            order_id = response.get("order_id") or "N/A"
            status_text = (
                f"reconcile: {status}\norder_id: {order_id}" if status else "reconcile: complete"
            )
            await update.message.reply_text(status_text, parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"Reconcile failed: {exc}")
            return

    async def assets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_needed(update):
            return
        response = await client.get("/account/assets")
        await update.message.reply_text(format_response(response), parse_mode="HTML")

    async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_needed(update):
            return
        params = {"symbol": context.args[0]} if context.args else None
        response = await client.get("/account/positions", params=params)
        await update.message.reply_text(format_response(response), parse_mode="HTML")

    async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_needed(update):
            return
        params: dict[str, object] = {}
        if context.args:
            params["symbol"] = context.args[0]
        if len(context.args) > 1:
            params["limit"] = int(context.args[1])
        response = await client.get("/account/orders", params=params or None)
        await update.message.reply_text(format_response(response), parse_mode="HTML")

    app = Application.builder().token(settings.telegram_bot_token.get_secret_value()).build()
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("preview", preview))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("sell", sell))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("reconcile", reconcile))
    app.add_handler(CommandHandler("assets", assets))
    app.add_handler(CommandHandler("positions", positions))
    app.add_handler(CommandHandler("orders", orders))
    app.run_polling()


def parse_preview_args(args: list[str]) -> dict[str, object]:
    if len(args) not in (5, 6):
        raise ValueError("Usage: /preview BUY AAPL 1 LIMIT 150 [USD]")
    side, symbol, quantity, order_type, limit_price, *rest = args
    return {
        "side": side.upper(),
        "symbol": symbol.upper(),
        "quantity": float(quantity),
        "order_type": order_type.upper(),
        "limit_price": None if limit_price.upper() == "NONE" else float(limit_price),
        "currency": rest[0].upper() if rest else "USD",
    }


def format_response(response: dict[str, object]) -> str:
    lines = []
    for key, value in response.items():
        if key == "tiger_preview" and value:
            value = "[preview returned]"
        lines.append(f"<b>{html.escape(str(key))}</b>: {html.escape(str(value))}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
