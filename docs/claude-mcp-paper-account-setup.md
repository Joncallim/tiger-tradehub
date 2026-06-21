# Claude MCP + Tiger Paper Account Setup

This guide assumes you are comfortable copying commands into a terminal, but you do not need to
know how the code works.

## What You Are Setting Up

TradeHub runs on your computer as a small local service.

- Claude talks to TradeHub through MCP.
- TradeHub talks to Tiger Brokers through Tiger's Python SDK.
- Your Tiger credentials stay on your computer.
- Dry-run mode is on by default, so no live order is placed while you are testing.

Use a Tiger paper account first. Do not switch to live trading until you have tested the full flow
and understand every setting.

## 1. Get The Project Onto Your Computer

Open a terminal and go to the folder where you keep projects:

```bash
cd ~/projects
git clone https://github.com/Joncallim/tiger-tradehub.git
cd tiger-tradehub
```

If you already have the folder, update it instead:

```bash
cd ~/projects/tiger-tradehub
git pull
```

## 2. Run The Setup Script

Run these commands from inside the `tiger-tradehub` folder:

```bash
./setup.sh
```

The script creates `.venv`, installs TradeHub with MCP support, and starts the local setup UI.

## 3. Create Your Local Settings File

Open the setup page:

```text
http://127.0.0.1:8787/setup
```

Click `Save .env`. The setup page creates `.env`, generates a strong TradeHub API token, and keeps
`TRADEHUB_DRY_RUN=true` by default.

Keep `TRADEHUB_DRY_RUN=true` for your first tests. In dry-run mode, TradeHub lets you preview and
confirm orders, but it does not place them with Tiger.

## 4. Add Tiger Paper Account Credentials

In Tiger's developer portal, create or find your OpenAPI credentials for a paper account.

You need:

- Tiger ID
- Tiger account number
- OpenAPI private key

Put them in the setup page, or edit `.env` manually:

```bash
TIGEROPEN_TIGER_ID=your-tiger-id
TIGEROPEN_ACCOUNT=your-paper-account-number
TIGEROPEN_PRIVATE_KEY_PATH=/absolute/path/to/your/private_key.pem
```

Use `TIGEROPEN_PRIVATE_KEY_PATH` if your key is saved as a file. If you put the key directly in
`.env`, use `TIGEROPEN_PRIVATE_KEY` instead, but a file path is easier to manage.

Important: make sure this is your Tiger paper account, not your live account.

## 5. Start TradeHub

If TradeHub is not already running, start it in one terminal window:

```bash
cd ~/projects/tiger-tradehub
./setup.sh
```

Leave this terminal running. TradeHub should listen on:

```text
http://127.0.0.1:8787
```

Open this page in a browser to confirm it is running:

```text
http://127.0.0.1:8787/setup
```

## 6. Test TradeHub Without Claude

Open a second terminal. Load the same API token from `.env` manually or paste it into the command:

```bash
export TRADEHUB_API_TOKEN=paste-your-generated-token-here
curl -s http://127.0.0.1:8787/health \
  -H "Authorization: Bearer $TRADEHUB_API_TOKEN"
```

You should see JSON with:

```json
{
  "ok": true,
  "dry_run": true
}
```

If this fails, fix it before connecting Claude.

## 7. Add TradeHub To Claude Desktop

Open `http://127.0.0.1:8787/setup` and use `Write MCP config`. This preserves any existing MCP
servers in Claude's config file.

Manual setup is still supported.

Find Claude Desktop's MCP configuration file.

Common locations:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add this block. Replace the path and token with your real values:

```json
{
  "mcpServers": {
    "tiger-tradehub": {
      "command": "/absolute/path/to/tiger-tradehub/.venv/bin/tradehub-mcp",
      "env": {
        "TRADEHUB_BASE_URL": "http://127.0.0.1:8787",
        "TRADEHUB_API_TOKEN": "paste-your-generated-token-here"
      }
    }
  }
}
```

The `command` must be an absolute path. For example:

```json
"command": "/Users/alex/projects/tiger-tradehub/.venv/bin/tradehub-mcp"
```

Restart Claude Desktop after saving the file.

## 8. Test Claude Tools In Dry-Run Mode

In Claude, ask:

```text
Check TradeHub health.
```

Then ask:

```text
Preview a dry-run limit order to buy 1 share of AAPL at 150 USD. Show me the exact details and
confirmation token. Do not submit until I explicitly say yes.
```

Claude should call the preview tool and show a confirmation token.

Then ask:

```text
Submit that confirmation token as a dry run.
```

The expected result is:

```text
submitted: false
dry_run: true
```

That means the workflow works, but no live paper order was placed.

## 9. Test Against Tiger Paper Account

After dry-run testing works, use your Tiger paper account:

1. Stop TradeHub with `Ctrl+C`.
2. Open `.env`.
3. Change:

```bash
TRADEHUB_DRY_RUN=false
```

4. Confirm your `TIGEROPEN_ACCOUNT` is the paper account.
5. Start TradeHub again:

```bash
tradehub
```

Ask Claude to preview a very small USD limit order. Keep the order small and use a limit price.

Only submit after checking:

- Symbol
- Side, such as BUY or SELL
- Quantity
- Limit price
- Account is your paper account
- Dry-run is false only because you intentionally want a paper-account test

## 10. Safety Checklist

Before using this with anything beyond paper testing:

- Keep the service bound to `127.0.0.1`.
- Keep your `.env` file private.
- Use a long random `TRADEHUB_API_TOKEN`.
- Start with `TRADEHUB_DRY_RUN=true`.
- Use a Tiger paper account first.
- Only use USD limit orders.
- Never ask Claude to submit without showing you the exact preview first.

## Troubleshooting

If Claude does not show TradeHub tools:

- Restart Claude Desktop.
- Check that the `command` path is absolute.
- Check that `tradehub` is running in a separate terminal.
- Use `Write MCP config` from the setup page again, or check that the token in Claude's config
  exactly matches `TRADEHUB_API_TOKEN` in `.env`.

If TradeHub says the token is invalid:

- Open `http://127.0.0.1:8787/setup`, save `.env`, then use `Write MCP config`.
- Restart both TradeHub and Claude.

If Tiger preview or submit fails:

- Confirm the Tiger credentials are for OpenAPI.
- Confirm the account is a paper account.
- Confirm the private key path is absolute and readable.
- Try `/health` first and check whether `tiger_configured` is `true`.
