---
name: tiger-tradehub
description: Use when working with Tiger TradeHub, the local guarded Tiger Brokers bridge, including safe finance workflows, MCP tool use, dry-run demos, order previews, security review, or feedback capture. Follow read-only-first, dry-run-first, preview-first, explicit-confirmation practices.
---

# Tiger TradeHub

## Core Positioning

Tiger TradeHub is a local-first, guarded bridge for AI-assisted brokerage workflows. It is not
investment advice, not an autonomous trading bot, and not a promise that AI can trade safely.

The intended posture is:

1. Read-only first.
2. Dry-run first.
3. Preview before submit.
4. Explicit user confirmation before any risky action.
5. Deterministic policy and audit logging outside the model.

## Before Using Tools

Always start with `health` if TradeHub tools are available.

Confirm and communicate:

- Whether TradeHub is reachable.
- Whether `dry_run` is active.
- Whether Tiger credentials are configured.
- Whether approval is required.

If the tools are unavailable, tell the user to start the local API with `tradehub` and connect the
`tradehub-mcp` server. Do not invent tool results.

## Safe Finance Workflow

When the user asks for financial analysis or trade preparation:

1. Separate research from execution.
2. State when data is missing, stale, or not source-backed.
3. Prefer read-only account, portfolio, position, buying-power, order-status, and market-data
   workflows when available.
4. Do not recommend buying or selling a security as financial advice.
5. If the user asks to place or prepare a trade, offer an order preview only.
6. Show exact order details and warnings before any confirmation step.

## Order Preview Rules

Use `preview_order` only when the user has supplied enough order intent:

- Symbol.
- Side.
- Quantity.
- Order type.
- Limit price for limit orders.
- Currency if not USD.

After preview, display:

- Symbol.
- Side.
- Quantity.
- Order type.
- Limit price if present.
- Estimated notional if available.
- Dry-run state.
- Policy warnings.
- Confirmation token.
- Token expiry.
- Whether Tiger preview was returned or skipped.

If policy blocks the request, explain the block plainly and do not try to route around it.

## Submit Rules

Never call `submit_order` casually.

Only submit when the user explicitly confirms the exact previewed order. A safe confirmation should
include either the confirmation token or an unambiguous instruction tied to the exact order details
already shown.

Before submitting, restate:

- The order details.
- The dry-run state.
- That live mode, if enabled, could place a real Tiger Brokers order.

If `dry_run` is true, explain that the submit should record the event but not place a live order.

## Cancel Rules

Use `cancel_order` only when the user provides a specific order id and explicitly asks to cancel it.

If dry-run mode is active, explain that the cancellation is recorded as a dry-run cancel.

## Security Review Workflow

When asked to review or improve TradeHub, focus first on:

- Authentication and token handling.
- Prompt injection and excessive agency.
- Confirmation token expiry, replay, and race behavior.
- Policy coverage.
- Audit completeness.
- Credential handling.
- Local-only defaults and public-exposure risks.
- Read-only endpoints before execution endpoints.
- Supply-chain posture, including CI, dependency updates, and static analysis.

Use the repo docs as primary context:

- `README.md`
- `docs/comparable-projects.md`
- `docs/marketing-input-program.md`
- `docs/mcp-vs-skill-decision.md`

## Feedback Capture

When gathering input, ask narrow questions:

- Security reviewer: "What control is missing before live-account use?"
- AI tooling builder: "Where should the assistant boundary be stricter?"
- Broker API user: "Where are the Tiger-specific assumptions wrong?"
- Financial research builder: "Which read-only data endpoint should come before execution?"
- Portfolio reviewer: "Does this demonstrate security-minded AI engineering clearly?"

Capture feedback as issues or roadmap items. Bias toward security, clarity, and read-only workflows
over broader execution features.

## Avoid

- Do not call the project an autonomous trading bot.
- Do not make profit claims.
- Do not provide personalized investment advice.
- Do not encourage live trading.
- Do not bypass policy checks.
- Do not submit orders without explicit user confirmation.
- Do not expose API tokens, confirmation tokens, Tiger credentials, or private keys in public docs,
  screenshots, logs, or demo materials.
