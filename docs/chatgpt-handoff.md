# Tiger TradeHub — Functional Acceptance Hand-off for ChatGPT

Date: 2026-08-23
Repo: https://github.com/Joncallim/tiger-tradehub
Tracker issue: #23 (Functional Acceptance Program — Hermes + DeepSeek V4 Flash)

## Where things stand

CORE FUNCTIONAL ACCEPTANCE NOT YET PASSED — one blocker on FA-05.

| Pack | Issue | Status | Evidence |
|------|-------|--------|----------|
| FA-00 runner + DeepSeek qualification | #24 | ✅ PASS (closed) | fa00-…31a13c7f @ e6a88d1; 11 fixtures incl. paper/live mismatch 100% |
| FA-01 local runtime preflight | #25 | ✅ PASS (closed) | fa01-…08db550a @ e6a88d1; 9/9 loopback/auth/approval/dry-run/no-leak |
| FA-02 MCP + read-only Tiger | #26 | ✅ PASS (closed) | fa02-…2abf2c70 @ fb3f3e3; 8/8 real MCP reads on paper account |
| FA-03 dry-run lifecycle via MCP | #27 | ✅ PASS (closed) | fa03-…aa432eb9 @ fb3f3e3; 7/7, 0 live_submit |
| FA-04 runtime safety/restart | #28 | ✅ PASS (closed) | fa04-…8316f1bf; 6/6 policy/replay/expiry/audit/sanitize |
| FA-05 paper broker lifecycle | #29 | ⛔ BLOCKED (open) | fa05-…e1574816 — US market-data permission missing |
| FA-06 deployment readiness | #30 | ⛔ BLOCKED (open) | no supervisor deployed yet; precondition note posted |
| FA-07 Telegram parity | #31 | ⛔ optional (open) | Telegram not configured as a production surface |

Current deployed commit: `3f844ee` (main, pushed). Lineage state in `data/acceptance/state.json`.

## FA-05 status (PASS 2026-08-24, core acceptance complete)

**CORE FUNCTIONAL ACCEPTANCE PASSED.** FA-05 uses Tiger's freely available **delayed** US quote (`get_stock_delay_briefs`) to derive a deterministic conservative paper-test limit (`delayed_price * 0.50`, shrunk by the runner-owned notional-cap rule when needed). The delayed quote is explicitly labelled DELAYED and never treated as current executable market data. Real-time US L1 is NOT required.

Verified FA-05 run: broker-reported `accountType=PAPER` (via `get_managed_accounts`) → delayed quote 309.35 → limit 80.00 (fraction shrunk to fit $100 cap) → one paper order placed (BUY 1 AAPL @ 80.00), read back (HELD), cancelled (`cancelled=true`), final CANCELLED filled=0, audit reconciled, exactly one new order, no fill, no duplicates.

Permission reference (kept for diagnosis only):
- `aStockQuoteLv1` = **China A-share L1** (unrelated to US quotes; the only permission this account holds)
- `usQuoteBasic` / `usStockQuote` = US real-time entitlements — **OPTIONAL**, not required for acceptance. Do not purchase merely for acceptance.

Rerun anytime:
```bash
env TRADEHUB_ACCEPTANCE_PAPER_WRITE=true .venv/bin/tradehub-acceptance run FA-05 --json
```

## How to operate the acceptance runner

```bash
# list packs
.venv/bin/tradehub-acceptance run --list
# run a pack (JSON result; runner owns retries/timeouts/status/sanitization)
.venv/bin/tradehub-acceptance run FA-00 --json
.venv/bin/tradehub-acceptance run FA-01 --json   # starts/stops real tradehub
.venv/bin/tradehub-acceptance run FA-02 --json   # real tradehub-mcp over stdio
.venv/bin/tradehub-acceptance run FA-03 --json   # dry-run lifecycle via MCP
.venv/bin/tradehub-acceptance run FA-04 --json   # restarts service (TTL=1s override instance)
env TRADEHUB_ACCEPTANCE_PAPER_WRITE=true .venv/bin/tradehub-acceptance run FA-05 --json
```

- Terminal states: PASS / FAIL / BLOCKED / ESCALATE. Unknown pack → FAIL (fail closed).
- Low-tier agent rules: run packs, read results, post concise evidence to the issue. Do NOT edit source/policy/credentials/criteria. Do NOT change runner arguments to rescue a BLOCKED/FAIL. Source fixes escalate to a stronger coding agent; the fixer never declares its own acceptance pass — the low-tier tester reruns independently.
- Artifacts: `data/acceptance/<run_id>.json` (sanitized), `data/acceptance/state.json` (lineage), service logs `data/acceptance/service-<run_id>.log`.

## Operational commands (this host)

```bash
# service (bare process, not Docker, loopback 127.0.0.1:8787)
.venv/bin/tradehub                          # start
# MCP server (used by FA-02/FA-03; env: TRADEHUB_BASE_URL, TRADEHUB_API_TOKEN)
.venv/bin/tradehub-mcp
# tests / gates (must stay green before any acceptance run)
.venv/bin/python -m pytest tests/ -q        # 41 tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .
# git (repo owned by uid 1000 — always sudo -u jon git ...)
sudo -u jon git status -sb
```

## Deployed environment notes

- TradeHub prod config lives in `.env` (root-owned): dry_run=true, allowlist `["AAPL","MSFT","VOO"]`, max notional $1000, max qty 100, TTL 300s, license TBSG, Tiger paper creds (tiger_id + 17-digit paper account 2115…, PKCS#8 key at data/tiger_private_key.pk8.pem).
- FA-05's write run uses `TRADEHUB_DRY_RUN=false` overridden on a dedicated acceptance instance only after the broker PAPER proof + usable delayed reference succeed; production `.env` is never modified.
- Key files: `tradehub/acceptance/` (runner: runner.py, schema.py, sanitize.py, service.py, mcp_client.py, state.py, packs/fa00..fa05), `docs/functional-acceptance-program.md` (canonical design), `docs/rate-limits.md`.
- Known rate limits: per tigerId+method — high 120/min, mid 60/min, low 10/min; place_order+modify_order shared 5 req/s per account; repeated 429s → account blacklist.

## Engineering notes from the run (for continuity)

- mcp 2.0 removed `FastMCP`; pyproject now pins `mcp>=1.2,<2.0` (b70c440) + import regression test. Don't "modernize" mcp_server.py blindly.
- Acceptance runner learns: never block on child pipe reads (use log files); never join hung threads (shutdown(wait=False)); resolve settings before building the sanitizer; check configured secrets, not env-var names, in leak assertions; share one service lifecycle across MCP assertions.
- Do NOT start V2 strategy/alpha testing; this task was plumbing acceptance only.