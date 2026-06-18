# Tiger TradeHub

Tiger TradeHub is a guarded local bridge from Claude to Tiger Brokers OpenAPI.
It is designed primarily for a local Claude MCP workflow: you run TradeHub on your own machine,
Claude gets a small set of trading tools, and Tiger credentials stay in your local environment.

TradeHub uses Tiger's official Python SDK for account, preview, and order placement, but keeps AI
clients behind explicit policy checks and a two-step confirmation flow.

Smooth onboarding is a project goal. A new user should be able to install the package, start in
dry-run mode, connect Claude, preview an order, and confirm a dry-run submission without exposing a
public trading endpoint.

## What This Builds

- Local `FastAPI` REST service used as the guarded backend.
- MCP server for Claude Desktop or Claude Code.
- Optional Telegram bot for `/buy`, `/sell`, `/preview`, `/confirm`, and `/health`.
- SQLite audit trail for previews, confirmations, submissions, and blocked requests.
- Dry-run mode enabled by default.
- OpenAPI schema for advanced direct HTTP or ChatGPT Actions deployments.

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

## Quick Start For Claude

This is the recommended path.

```bash
cd tiger-tradehub
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[mcp]"
cp .env.example .env
```

Edit `.env` and set a strong local API token:

```bash
TRADEHUB_API_TOKEN=replace-with-a-long-random-token
TRADEHUB_DRY_RUN=true
```

Generate a token with:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Dry-run mode does not place live Tiger orders. Keep it enabled until the full Claude preview and
confirmation flow is verified.

Tiger credentials are only required when you are ready to call Tiger's preview/order APIs:

Tiger credentials come from Tiger's developer portal:

- `TIGEROPEN_TIGER_ID`
- `TIGEROPEN_ACCOUNT`
- RSA private key, either as `TIGEROPEN_PRIVATE_KEY_PATH` or `TIGEROPEN_PRIVATE_KEY`

## Run TradeHub

```bash
source .venv/bin/activate
tradehub
```

Open:

- API docs: `http://127.0.0.1:8787/docs`
- OpenAPI schema: `http://127.0.0.1:8787/openapi.json`

Check the API:

```bash
curl -s http://127.0.0.1:8787/health \
  -H "Authorization: Bearer $TRADEHUB_API_TOKEN"
```

## Connect Claude

Run the API first, then add this MCP server to Claude Desktop or Claude Code config:

```json
{
  "mcpServers": {
    "tiger-tradehub": {
      "command": "/absolute/path/to/tiger-tradehub/.venv/bin/tradehub-mcp",
      "env": {
        "TRADEHUB_BASE_URL": "http://127.0.0.1:8787",
        "TRADEHUB_API_TOKEN": "replace-with-the-same-token-from-.env"
      }
    }
  }
}
```

Use the absolute path to `tradehub-mcp` from this checkout. For example, if the repo is in
`/root/tiger-tradehub`, use:

```json
"command": "/root/tiger-tradehub/.venv/bin/tradehub-mcp"
```

Restart Claude after editing the config. Claude should then have TradeHub tools for health checks,
order previews, order submission, and cancellation.

The MCP tools call TradeHub's guarded REST API; they do not talk to Tiger directly.

## First Dry-Run Flow

Ask Claude to:

1. Check TradeHub health.
2. Preview a small limit order, for example one share of `AAPL`.
3. Show the confirmation token and exact order details.
4. Submit only after you explicitly confirm.

Expected result in dry-run mode: TradeHub records the preview and confirmation, but returns
`submitted: false` and does not place a live Tiger order.

## Example REST Calls

The REST API is the shared backend behind the Claude MCP server. These calls are useful for debugging
or automation.

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

## ChatGPT Actions

For ChatGPT Actions, the service must be reachable from ChatGPT. In practice that means deploying it
behind HTTPS or exposing it through a carefully controlled tunnel. Do not expose it publicly without
`TRADEHUB_API_TOKEN`, IP allowlisting, and dry-run/paper-account testing first.

This project is not currently designed as a central multi-user ChatGPT Actions service. The current
architecture assumes one local user, one Tiger account, one API token, and a local SQLite audit log.
If ChatGPT support is needed, prefer a per-user deployment rather than a shared central server.

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
