# Tiger TradeHub

Tiger TradeHub is a guarded local bridge from ChatGPT, Claude, or Telegram to Tiger Brokers OpenAPI.
It uses Tiger's official Python SDK for account, preview, and order placement, but keeps LLM and chat
clients behind explicit policy checks and a two-step confirmation flow.

## What This Builds

- `FastAPI` REST service with an OpenAPI schema for ChatGPT Actions or direct HTTP calls.
- Optional MCP server for Claude Desktop or Claude Code.
- Optional Telegram bot for `/buy`, `/sell`, `/preview`, `/confirm`, and account checks.
- SQLite audit trail for previews, confirmations, submissions, and blocked requests.
- Dry-run mode enabled by default.

Tiger's official OpenAPI supports account status, order creation/modification/cancellation, market
data, streaming push updates, and paper accounts. Their Python SDK exposes order preview and order
placement helpers; see the official SDK and docs linked below.

## Safety Model

The service is deliberately not a raw trading proxy.

1. A client submits an order intent to `/orders/preview`.
2. TradeHub validates symbol allowlist, quantity, notional, order type, and side.
3. TradeHub returns a confirmation token and, when configured, Tiger's own order preview.
4. The client must call `/orders/submit` with that token before it expires.
5. If `TRADEHUB_DRY_RUN=true`, no live Tiger order is placed.

Keep `TRADEHUB_DRY_RUN=true` and use a Tiger paper account until you have verified the full flow.

## Setup

```bash
cd /Users/jonathanlim/Documents/Investment
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[mcp,telegram,dev]"
cp .env.example .env
```

Edit `.env` with your Tiger developer credentials and a strong `TRADEHUB_API_TOKEN`.

Tiger credentials come from Tiger's developer portal:

- `TIGEROPEN_TIGER_ID`
- `TIGEROPEN_ACCOUNT`
- RSA private key, either as `TIGEROPEN_PRIVATE_KEY_PATH` or `TIGEROPEN_PRIVATE_KEY`

## Run The API

```bash
source .venv/bin/activate
tradehub
```

Open:

- API docs: `http://127.0.0.1:8787/docs`
- OpenAPI schema for ChatGPT Actions: `http://127.0.0.1:8787/openapi.json`

For ChatGPT Actions, the service must be reachable from ChatGPT. In practice that means deploying it
behind HTTPS or exposing it through a carefully controlled tunnel. Do not expose it publicly without
`TRADEHUB_API_TOKEN`, IP allowlisting, and dry-run/paper-account testing first.

## Example REST Calls

```bash
curl -s http://127.0.0.1:8787/health \
  -H "Authorization: Bearer $TRADEHUB_API_TOKEN"
```

```bash
curl -s http://127.0.0.1:8787/orders/preview \
  -H "Authorization: Bearer $TRADEHUB_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","side":"BUY","quantity":1,"order_type":"LIMIT","limit_price":150,"currency":"USD"}'
```

```bash
curl -s http://127.0.0.1:8787/orders/submit \
  -H "Authorization: Bearer $TRADEHUB_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"confirmation_token":"token-from-preview"}'
```

## Claude MCP

Run the API first, then add this MCP server to Claude's config:

```json
{
  "mcpServers": {
    "tiger-tradehub": {
      "command": "tradehub-mcp",
      "env": {
        "TRADEHUB_BASE_URL": "http://127.0.0.1:8787",
        "TRADEHUB_API_TOKEN": "your-token"
      }
    }
  }
}
```

The MCP tools call TradeHub's guarded REST API; they do not talk to Tiger directly.

## Telegram

Create a bot with BotFather, then set:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Run:

```bash
tradehub-telegram
```

Commands:

- `/preview BUY AAPL 1 LIMIT 150`
- `/buy AAPL 1 150`
- `/sell AAPL 1 150`
- `/confirm <token>`
- `/health`

## Sources

- Tiger OpenAPI introduction: <https://quant.itigerup.com/openapi/en/python/overview/introduction.html>
- Tiger Python SDK: <https://github.com/tigerfintech/openapi-python-sdk>
- Tiger Python quickstart/order example: <https://quant.itigerup.com/openapi/en/python/quickStart/basicFunction.html>

See [docs/comparable-projects.md](docs/comparable-projects.md) for a scan of similar GitHub projects.
