# Tiger TradeHub — Functional Acceptance Hand-off for ChatGPT

Date: 2026-08-23
Repo: https://github.com/Joncallim/tiger-tradehub (local: /home/jon/tiger-tradehub)
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

## The single blocker (FA-05 / #29)

**Corrected diagnosis (2026-08-23):** the account's only market-data permission is `aStockQuoteLv1`, which is **China A-share L1** — NOT US market data. Tiger's official permission table (docs-en.itigerup.com/docs/quote-common.md) defines the US entitlements as:
- `usQuoteBasic` — US stock L1 market data access
- `usStockQuote` — US real-time stock market data access

`get_quote_permission()` / `grab_quote_permission()` currently return ONLY `[{'name': 'aStockQuoteLv1', 'expire_at': -1}]`. With no `usQuoteBasic`/`usStockQuote`, every US quote call (`get_briefs`, `quote_real_time`, `timeline`, `trade_tick`) correctly fails with `ApiException code=4 msg=4000: permission denied (…US market)`.

Per the acceptance spec, FA-05 must **deterministically prove the test limit is non-marketable from a current quote** before placing the order. Without US L1 quotes that proof is impossible → the runner correctly returns **BLOCKED** and never places an order (verified: no broker write occurred).

Note: PAPER is already positively proven ($1,000,000 paper balance, broker-reported accountType=PAPER via get_managed_accounts) — this is NOT an account-type problem. It is purely the missing US quote entitlement.

### Activation route (for Jon)

1. **Developer Center:** https://developer.itigerup.com/profile → API market data / quota → **US L1** (yields `usQuoteBasic`); real-time US market data must be purchased/enabled separately (docs: `permission.md`).
   Or **Tiger Trade app:** Profile → **Market Data Store** → **API** → **US L1**.
2. **Verify after activation** (one call, reuse a single module-level QuoteClient — `grab_quote_permission()` transfers device access and does NOT purchase permission; only one device can hold access at a time; repeated grabs from fresh clients cause device-access contention):
   ```bash
   cd /home/jon/tiger-tradehub
   .venv/bin/python - <<'EOF'
   from tigeropen.common.consts import Language
   from tigeropen.common.util.signature_utils import read_private_key
   from tigeropen.quote.quote_client import QuoteClient
   from tigeropen.tiger_open_config import TigerOpenClientConfig
   from tradehub.config import get_settings
   s = get_settings()
   config = TigerOpenClientConfig(sandbox_debug=s.tiger_sandbox)
   config.tiger_id = s.tiger_id or ""; config.account = s.tiger_account or ""
   if s.tiger_license: config.license = s.tiger_license
   config.language = Language.en_US
   config.private_key = read_private_key(str(s.tiger_private_key_path))
   client = QuoteClient(config)  # ONE instance, reused
   print("perms:", client.grab_quote_permission())
   print("briefs:", client.get_briefs(["AAPL"]))
   EOF
   ```
   Expected now: `perms` includes `usQuoteBasic`/`usStockQuote`, and `briefs` returns AAPL prices.
   If the US entitlement appears but quote calls STILL return code 4000 → investigate **device-access contention** next (only one device holds real-time quote access; grab transfers it).
3. Only then rerun:
   ```bash
   env TRADEHUB_ACCEPTANCE_PAPER_WRITE=true .venv/bin/tradehub-acceptance run FA-05 --json
   ```
   Expected: gates PASS → lifecycle places ONE tiny non-marketable limit BUY (AAPL, qty 1, limit = 50% of market), reads it back, cancels it, reconciles audit, proves exactly one order.
4. Post result to #29; if PASS, close it and update #23.

**Do not place any broker order until FA-05 independently proves PAPER account + current quote + non-marketable limit.**

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
- FA-05's write run uses `TRADEHUB_DRY_RUN=false` overridden on a dedicated acceptance instance only after the PAPER + non-marketable proofs pass; production `.env` is never modified.
- Key files: `tradehub/acceptance/` (runner: runner.py, schema.py, sanitize.py, service.py, mcp_client.py, state.py, packs/fa00..fa05), `docs/functional-acceptance-program.md` (canonical design), `docs/rate-limits.md`.
- Known rate limits: per tigerId+method — high 120/min, mid 60/min, low 10/min; place_order+modify_order shared 5 req/s per account; repeated 429s → account blacklist.

## Engineering notes from the run (for continuity)

- mcp 2.0 removed `FastMCP`; pyproject now pins `mcp>=1.2,<2.0` (b70c440) + import regression test. Don't "modernize" mcp_server.py blindly.
- Acceptance runner learns: never block on child pipe reads (use log files); never join hung threads (shutdown(wait=False)); resolve settings before building the sanitizer; check configured secrets, not env-var names, in leak assertions; share one service lifecycle across MCP assertions.
- Do NOT start V2 strategy/alpha testing; this task was plumbing acceptance only.