# Tiger TradeHub

A simple, low-touch autonomous investment system: deterministic research,
guarded execution, honest validation, and concise reporting. The ambition is
a **boring, reliable daily operating system** — models interpret where they
materially help; deterministic code does the rest. **Not financial advice.**
MIT License. Dry-run mode is on by default; use a Tiger paper account.

## Architecture (V2)

```
market/evidence refresh          (ops timers: Tiingo/SEC incremental)
        ↓
deterministic Hunters            (momentum/valuation/quality/inflection/event)
        ↓
candidate funnel + evidence packs
        ↓
committee — models ONLY where interpretation materially helps
        ↓
deterministic score / state / risk
        ↓
typed proposals → deterministic execution policy → guarded execution
        ↓
broker reconciliation → deterministic daily/weekly reporting
```

Three trust contexts stay distinct:

| Context | What it sees | Where |
|---|---|---|
| **Committee** | Raw evidence packs, 3 small MCP tools (`get_evidence_pack`, `submit_assessment`, `committee_status`) — no execution, no credentials, no shell | `tradehub-research-mcp` (Hermes) |
| **Operator / read-only** | Sanitized research/portfolio/report summaries | deterministic CLI + reports |
| **Execution / approval** | Tiger credentials, broker write authority, confirmation flow | `tradehub` service, `tradehub-execution` user, loopback-only |

Models are **never** the privileged autonomous execution actor. The future
autonomous runner is deterministic code inside a human-defined constitution
(Phase 6 / #51 — paper only).

## Research plane

- **Deterministic Hunters** screen the eligible universe with PIT-correct
  evidence (`public_available_time <= as_of`). Raw/unadjusted fields for
  decision features; adjusted values live in an audit-only namespace.
- **BOOTSTRAP_COHORT** discipline: the 450-ticker sample (seed 20260827) is
  a present-day cohort, never a historical PIT universe. Pre-bootstrap
  dates are legitimately empty.
- **Validation engine** (Phase 5): frozen snapshots, append-only experiment
  ledger, sealed one-time holdout, honest `INSUFFICIENT DATA` verdicts.
  VALIDATION ENGINE = PASS; INVESTMENT EVIDENCE = INSUFFICIENT DATA until
  real forward outcomes mature.
- **Forward tracker**: every actual production screen is recorded as an
  immutable prediction (`as_of <= collection date` enforced; future-dated
  rows rejected); outcomes are appended as they mature.

## Daily operation (systemd timers, no scheduler framework)

| Timer | When (UTC) | Job |
|---|---|---|
| `tradehub-daily-refresh` | Mon–Fri 22:40 | bounded Tiingo/SEC incremental (45/hr quota, resumable) |
| `tradehub-research-cycle` | Mon/Wed/Fri 23:00 | freshness → universe → Hunters → funnel → scoring → proposals |
| `tradehub-forward-capture` | Mon–Fri 23:30 | genuine forward predictions (PASS/FAIL/insufficient, idempotent) |
| `tradehub-outcome-maturation` | Mon–Fri 23:45 | append outcomes for due horizons; predictions never modified |

Reports: deterministic daily (23:00) + weekly (Fri) delivered via
Hermes/Telegram. **No model calculates P&L**; missing broker values render
`unavailable`, never `$0`. Tiger account analytics are the accounting source
of truth; no shadow brokerage ledger.

## Guarded execution

- Loopback-only REST (`127.0.0.1:8787`), bearer token, dry-run default,
  symbol allowlist + notional caps, preview → confirmation-token → submit.
- Execution credentials exist only in the execution context
  (`/etc/tradehub/execution.env`, `tradehub-execution` user). Research has
  **zero** Tiger credentials.
- `ProtectHome`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/tradehub`.

## Deployment

- `/opt/tiger-tradehub` — deployed code (pinned commit in `DEPLOYED_COMMIT`)
- `/var/lib/tradehub` — execution state · `/var/lib/tradehub-research` — research state
- Services: `tradehub-execution.service`, `tradehub-research.service`
  (committee API on `127.0.0.1:8091`), plus the four timer units above.
- Acceptance: `deploy/fa06_acceptance.py` (start/restart/persistence/
  rollback/secrets — 19/19 on the live host).

## Operator / reporting

- `python -m tradehub_research.ops.health` — forward-ledger + market-freshness health
- `python -m tradehub_research.ops.report_cli --period daily|weekly` — deterministic report text
- Research APIs: committee API `127.0.0.1:8091` (read-only evidence/assessment surface).

## Docs

Detailed operator material (deployment, acceptance, migration, validation
methodology) lives in `docs/`; this README stays a simple orientation.
