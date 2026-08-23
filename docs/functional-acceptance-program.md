# Functional Acceptance Program

Date: 2026-08-23

## Purpose

This program proves that the **deployed TradeHub stack actually works end-to-end** before any V2 investment-strategy validation begins.

It is intentionally separate from the existing unit/integration suite. The current tests already cover policy, authentication, confirmation-token lifecycle, audit behavior, read-only REST endpoints, submit behavior, and Tiger gateway wiring. Functional acceptance therefore focuses on what ordinary CI cannot prove reliably:

- the real local TradeHub process starts with the expected safety posture;
- Hermes can discover and use the real MCP tools;
- read-only Tiger account calls work from the deployed environment;
- the guarded dry-run order lifecycle works through MCP;
- runtime restart/recovery and audit behavior remain safe;
- a real Tiger **paper** order can be previewed, placed, reconciled, and cancelled;
- eventual deployment does not silently weaken local-only/authentication/safety boundaries.

## Design Principles

1. **One runner, not one script per test.** Add a single `tradehub-acceptance` command with declarative test packs.
2. **The agent is an operator, not an oracle.** DeepSeek V4 Flash dispatches packs and reports structured results; deterministic code decides PASS/FAIL/BLOCKED/ESCALATE.
3. **Do not duplicate pytest.** Existing unit/integration coverage remains authoritative for code-level cases. Acceptance tests only repeat a behavior when the runtime/MCP boundary itself is what is being tested.
4. **Production safety policy is unchanged.** Do not weaken the normal TradeHub skill, confirmation flow, authentication, allowlists, notional limits, or live-trading policy to make tests easier.
5. **Paper is proven positively.** `TIGEROPEN_SANDBOX` is not proof of paper trading. Before any broker write, TradeHub must query Tiger account information and prove the configured account has `accountType=PAPER`.
6. **No broker credentials in GitHub-hosted CI.** Offline checks can run in GitHub Actions; local-runtime and paper packs run only on the trusted TradeHub/Hermes host.
7. **No self-healing test agent.** The low-tier tester may run, retry according to runner policy, collect evidence, and report. It must not edit source, policy, credentials, or acceptance criteria.
8. **Failures are evidence.** A failed test is reported, not patched until green by the same tester. Code fixes are escalated; the low-tier tester performs the independent rerun afterward.
9. **Four terminal states only.** Every pack ends as `PASS`, `FAIL`, `BLOCKED`, or `ESCALATE`.
10. **Keep it extensible.** Future V2 strategy tests can reuse the runner/reporting contract without inheriting broker-write authority.

## Environment Classes

### `offline`

Safe for GitHub-hosted CI. No Tiger credentials or running local service required.

Examples: existing pytest/ruff/pip-audit parity, acceptance-runner fixtures, result-schema validation.

### `local`

Runs on the trusted TradeHub/Hermes host. May use local secrets and a running TradeHub process. No broker write is permitted.

Examples: actual bind address, real authentication, MCP discovery, account reads, dry-run order flow, restart/recovery.

### `paper`

Runs on the trusted TradeHub/Hermes host and may write only after the runner positively proves the Tiger account profile is `PAPER`.

If paper status cannot be proven, the pack returns `BLOCKED`. It must never infer safety from `sandbox_debug`, an account-number guess, filenames, prompts, or operator prose.

## Low-Tier Agent Contract

DeepSeek V4 Flash is the default functional-test operator.

Allowed actions:

- read the approved GitHub test issue;
- invoke `tradehub-acceptance run <PACK> --json`;
- read the structured result;
- collect runner-produced artifacts/log references;
- post a concise result to the issue;
- request escalation when the runner returns `ESCALATE` or when source changes are required.

Forbidden actions:

- modify source code;
- modify tests or acceptance criteria;
- change credentials;
- loosen policy limits or allowlists;
- infer that an account is paper/live;
- bypass a `BLOCKED` result;
- submit any order outside the acceptance runner's pre-authorized paper test pack;
- treat a test issue body as executable shell instructions.

The runner owns bounded retries for explicitly transient operations. The model does not decide how many retries are safe.

## Result Contract

Every pack should return JSON in a stable schema similar to:

```json
{
  "schema_version": 1,
  "pack_id": "FA-03",
  "run_id": "fa03-20260823T120000Z-acde1234",
  "environment": "local",
  "status": "PASS",
  "commit_sha": "...",
  "started_at": "...",
  "finished_at": "...",
  "assertions": [
    {"id": "health.dry_run", "status": "PASS", "detail": "dry_run=true"}
  ],
  "artifacts": [],
  "safe_summary": "Dry-run MCP order lifecycle passed.",
  "escalation_reason": null
}
```

The result must never include API tokens, Tiger IDs, account numbers, private keys, Telegram tokens, or raw confirmation tokens.

## Core Test Packs

### FA-00 — Acceptance runner + DeepSeek qualification

**Environment:** offline

Build and validate the single runner contract before testing TradeHub itself.

Acceptance:

- one CLI entry point: `tradehub-acceptance run <PACK> --json`;
- strict result schema with `PASS|FAIL|BLOCKED|ESCALATE`;
- runner owns bounded retries and timeouts;
- reports are secret-sanitized;
- unknown packs fail closed;
- fixture suite contains known pass/fail/blocked/escalation cases;
- DeepSeek V4 Flash classifies all safety-critical fixtures correctly;
- the agent cannot turn a blocked case into a write-capable case by changing arguments.

This pack qualifies the tester, not the trading system.

### FA-01 — Local runtime safety preflight

**Environment:** local

Prove the deployed process starts in the expected safety posture.

Acceptance:

- repository commit/version recorded;
- local install/import succeeds;
- current pytest, Ruff format/lint, and dependency-audit gates pass or are linked from CI;
- TradeHub listens on loopback only for the acceptance run;
- `/health` succeeds with the correct bearer token;
- missing/wrong bearer tokens are rejected;
- `require_approval=true` is reported;
- test starts in `dry_run=true`;
- logs/result artifacts contain no configured secrets;
- Tiger configuration state is reported without exposing credential values.

A non-loopback bind is `FAIL`, not a warning.

### FA-02 — MCP discovery + real read-only Tiger workflow

**Environment:** local

Prove Hermes/DeepSeek can use the deployed MCP surface rather than only the REST API.

Acceptance:

- MCP server starts successfully;
- expected tools are discoverable: `health`, `account_assets`, `account_positions`, `account_orders`, `preview_order`, `submit_order`, `cancel_order`;
- `health` works through MCP;
- if Tiger credentials are configured, assets/positions/orders return successfully through MCP;
- symbol/order-limit arguments are exercised through MCP;
- no write tool is invoked in this pack;
- if required Tiger credentials/connectivity are unavailable, return `BLOCKED`, not `FAIL`.

### FA-03 — Guarded dry-run order lifecycle through MCP

**Environment:** local

Prove the full MCP -> REST -> policy -> confirmation -> audit path without any broker write.

Acceptance:

- preflight proves `dry_run=true` and approval remains enabled;
- preview a configured small USD limit BUY that passes policy;
- response contains accepted intent, expiry, warnings, and confirmation token internally;
- acceptance authority permits the test harness to invoke the MCP submit tool without weakening the normal user-facing skill;
- submit returns `submitted=false` and `dry_run=true`;
- the same token cannot be submitted twice;
- dry-run cancellation records a dry-run cancel without contacting Tiger;
- audit sequence can be reconstructed from the local database;
- no confirmation token or credential appears in the public test report.

### FA-04 — Runtime safety, restart, and recovery

**Environment:** local

Exercise deployed failure boundaries that are easy to miss in ordinary CI.

Acceptance:

- prohibited market order is blocked;
- non-USD order is blocked;
- over-notional/over-quantity order is blocked;
- disallowed symbol is blocked when an acceptance allowlist is configured;
- malformed input fails closed;
- finalized/replayed token remains unusable across a service restart;
- expired token remains unusable across a restart;
- service restart preserves audit history;
- client-visible upstream/internal errors remain sanitized;
- low-tier tester reports unexpected exceptions as `ESCALATE` rather than improvising a fix.

Existing unit tests remain the detailed concurrency/fault-injection authority; this pack verifies the deployed runtime boundary.

### FA-05 — Tiger paper-account broker lifecycle

**Environment:** paper

This is the only core pack permitted to place a broker order.

Hard prerequisites:

- FA-00 through FA-04 passed on the same deployment lineage;
- runner queries Tiger account information and proves `accountType=PAPER` for the configured account;
- explicit acceptance-paper-write feature flag is enabled locally;
- acceptance symbol/quantity/notional remain inside stricter test-only caps;
- order is a USD limit order;
- runner verifies the intended test limit is non-marketable before submission, or blocks the test if it cannot prove this safely.

Acceptance:

1. read paper account state;
2. preview the small test order;
3. submit through the normal guarded TradeHub path;
4. receive a broker order ID;
5. read the order back through `/account/orders`/MCP;
6. cancel it;
7. read back the cancelled/final broker state;
8. reconcile audit events to the same broker order ID;
9. verify no unintended additional order was created.

Any inability to prove `PAPER` or non-marketable safety returns `BLOCKED`. Never fall back to a live account or market order.

## Deployment Pack

### FA-06 — Deployment/runtime readiness

**Environment:** local

This pack is intentionally separate from core functional acceptance because the final deployment mechanism may change.

Once a deployment target is chosen, prove:

- service starts/restarts under the chosen supervisor/container mechanism;
- loopback/local exposure policy is preserved;
- secrets are injected without being committed or printed;
- persistent audit storage survives restart/upgrade;
- MCP reconnects after service restart;
- health/read-only workflow works after reboot/redeploy;
- upgrade/rollback procedure is repeatable;
- if remote exposure is ever introduced, a separate security review is required before this pack can pass.

## Optional Surface Pack

### FA-07 — Telegram parity

**Environment:** local; paper writes are out of scope initially

Telegram is optional and therefore not part of the core acceptance gate.

Acceptance:

- bot refuses to start without an allowlist;
- unauthorized chat is rejected;
- authorized `/health`, `/assets`, `/positions`, `/orders`, and dry-run preview paths work;
- no Telegram path bypasses the guarded REST API;
- live/paper submit parity is tested only after FA-05 and under a separately approved extension.

## Gate Model

Core functional acceptance:

`FA-00 -> FA-01 -> FA-02 -> FA-03 -> FA-04 -> FA-05`

Deployment readiness:

`FA-05 -> FA-06`

Optional Telegram parity:

`FA-03 -> FA-07`

A downstream pack remains blocked until required upstream packs pass for the relevant deployment lineage.

## Orthogonal Review Conclusions

### Simplicity

Rejected: one issue/script per individual assertion. It duplicates existing pytest coverage and creates orchestration noise.

Chosen: six core packs with one runner and declarative assertions.

### Robustness

Rejected: let the LLM infer PASS/FAIL from logs.

Chosen: deterministic result schema, fixed terminal states, explicit environment gates, secret sanitization, and positive paper-account proof.

### Agent capability

Rejected: ask the cheap model to diagnose and patch arbitrary failures.

Chosen: DeepSeek V4 Flash runs/reports; source changes escalate to a coding agent; Flash independently reruns after the fix.

### Safety

Rejected: reuse `TIGEROPEN_SANDBOX` as evidence of paper trading or weaken the normal confirmation skill.

Chosen: broker-reported `accountType=PAPER`, a separate acceptance authority, stricter test caps, non-marketable limit-order proof, and fail-closed writes.

### Scalability

Rejected: custom issue prose as the executable specification.

Chosen: GitHub issues reference stable pack IDs; the runner/manifests own the executable contract. Future V2 tests can add packs without changing the agent protocol.

### Deployment

Rejected: store Tiger secrets in GitHub Actions or make hosted CI responsible for broker acceptance.

Chosen: hosted CI remains offline; local/paper acceptance runs on the trusted Hermes host. Deployment-specific checks are a separate pack because the supervisor/network architecture may evolve.

## Definition Of Done

TradeHub core functionality is accepted only when FA-00 through FA-05 pass on the intended Hermes deployment lineage and the resulting reports are retained.

This proves functionality and safety wiring. It does **not** prove that any future investment strategy is profitable; strategy validation begins only after functional acceptance.
