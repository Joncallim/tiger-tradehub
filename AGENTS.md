# Tiger TradeHub Agent Guide

## Purpose

Tiger TradeHub is guarded local trading infrastructure: a FastAPI execution
core, MCP client surface, optional Telegram bot, audit store, deterministic
acceptance runner, and a separate research plane. Safety and operator control
take precedence over convenience or successful order placement.

## Read First

Read `README.md`, `SECURITY.md`, `docs/threat-model.md`, and the affected code and
tests. Research-plane work also requires `docs/v2-architecture.md` and the current
phase handoff. Acceptance work follows `docs/functional-acceptance-program.md`.
Agent workflow and authority boundaries are in
`docs/agentic-implementation-policy.md`.

## Non-Negotiable Invariants

- Dry-run is the default. Use paper accounts until the full guarded flow is
  independently verified. Never place a live order without explicit user
  authority for that exact action.
- Preserve preview -> policy validation -> short-lived single-use confirmation
  -> submit. Re-run policy at submit; never create a raw broker proxy.
- Keep preview and execution capabilities distinct. Never expose execution
  credentials to preview-only or research processes.
- Default binding remains loopback. Public or shared multi-user exposure is not
  an ordinary deployment mode.
- Preserve symbol, side, type, currency, quantity, and notional restrictions.
  Release 1 remains USD limit orders unless architecture explicitly changes.
- Interrupted or ambiguous submissions fail closed and remain non-retryable
  until reconciliation proves an outcome. Never guess whether an order landed.
- Preserve complete, sanitized audit lineage without logging credentials,
  confirmation capabilities, or private broker material.
- Telegram access fails closed to explicitly allowed chat IDs.
- Keep `tradehub_research` separated from the execution plane; research output
  cannot submit orders or import execution authority.

## Repository Map

- `tradehub/app.py`: FastAPI surface and authentication.
- `tradehub/policy.py`: execution policy gates.
- `tradehub/audit.py`: confirmation state and audit persistence.
- `tradehub/tiger_gateway.py`: official Tiger SDK boundary.
- `tradehub/mcp_server.py`, `tradehub/telegram_bot.py`: guarded client adapters.
- `tradehub/acceptance`: deterministic execution-core acceptance runner.
- `tradehub_research`: separate research pipeline, committee, portfolio, and
  acceptance surface.
- `tests`: unit and integration coverage.
- `deploy`: deployment definitions; treat as externally visible infrastructure.

## Focused Routing And Ownership

- Policy, confirmation, submission, or reconciliation: one backend implementer,
  one security/adversarial reviewer, and an independent acceptance runner.
- Broker SDK work: one integration owner plus failure/idempotency review; mock in
  ordinary tests.
- MCP, REST, or Telegram adapters: one adapter owner plus auth/capability-boundary
  review.
- Research work: one research-domain owner plus methodology/data-lineage QA;
  independently verify that execution-plane imports and capabilities remain
  absent.
- Deployment work: one operations owner plus network, secret, and rollback review.
- One writer per file. The final acceptance agent must not be the author of the
  material fix and must not alter criteria or policy to rescue a failure.

## Validation

The deterministic code gate is:

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pip_audit
```

Match acceptance to the changed plane. For execution-core changes, use
`tradehub-acceptance run --list` and run the applicable approved FA pack. For
research-plane changes, use `tradehub-research-acceptance run --list` and run
the applicable RA-00 through RA-04 pack. Changes crossing both planes require
both applicable acceptance paths. Normal offline and dry-run acceptance may be
used when configured with synthetic or sanctioned local credentials. Broker
PAPER-write acceptance is not a routine test: it requires explicit authority
and every gate defined by the acceptance program. Never modify production
`.env` to run acceptance.

## Risk Triggers

Always require security/adversarial review for auth, tokens, policy, audit DB or
migrations, confirmation lifecycle, order state, reconciliation, broker calls,
network binding, Telegram authorization, logs, deployment, or research/execution
boundaries. Stop for human direction on credentials/2FA, external account
actions, irreversible infrastructure, live-broker ambiguity, or changes to the
investment and safety constitution. A blocked acceptance result is evidence to
escalate, not permission to loosen a guardrail.
