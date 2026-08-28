# Phase 5 — Data-Sufficiency Audit (Packet A)

Status: **living audit — programmatically re-derivable via
`research-validate audit`** (tradehub_research/validation/coverage_audit.py).
This document records the audit's evolution; the tool output is the oracle.

## Audit at start of #38 (main @ c200639)

Inspected the live `research.db` in this worktree and the sibling execution
worktree directly via `sqlite3`, checked adapter-level durable-quota state,
checked both `.env` files for Tiingo/SEC credentials, and checked for any
cron/systemd ingestion job.

| Check | Result (start) |
| --- | --- |
| `research.db` row counts (all core tables) | **0 in every table, both worktrees** |
| `TIINGO_TOKEN` configured | **No** |
| `RESEARCH_SEC_USER_AGENT` configured | **No** |
| Tiingo `bootstrap_symbol` rolling-30-day quota table | **Empty** — zero Tiingo API calls ever made |
| SEC EDGAR adapter | Exists, never invoked against live data |
| Cron/systemd ingestion | None found |

Conclusion: every "PARTIAL/EVALUABLE" posture in the handoff's §4 coverage
table was **ZERO-EVALUABLE** — a complete absence of ingested data, not a
coverage gap. `INSUFFICIENT DATA` remains a valid Phase-5 verdict.

## Update — SEC identity bootstrap (2026-08-27)

Per the steering directive and handoff §4.1, the SEC bulk route was used for
identity bootstrap (no key required; User-Agent
`TigerTradeHub joncallim@gmail.com`):

- `company_tickers.json` fetched once (bounded, cached, HTTP 200): **10,388
  companies** parsed (ticker + CIK + title).
- Hash-selected deterministic sample frozen into `experiment.db`
  `universe_sample` before any price retrieval: **450 tickers**, seed
  20260827, algorithm `sha256(seed+NUL+ticker) ascending take-450`,
  labeled **BOOTSTRAP_COHORT** (present-day sample, NOT a historical PIT
  universe; never promoted without reconstructed PIT membership).
- Security bootstrap into live `research.db`: **328 unique securities**
  (450 CIK-normalized rows, share-class CIK dedup), 450 baseline identity
  events, 328 eligible universe memberships (price/market-cap/liquidity
  NULL until Tiingo fills them). `knowledge_time` = retrieval time —
  documented PIT limitation: no historical constituent-index membership.

Live audit after bootstrap (`research-validate audit`):

| Check | Result (now) |
| --- | --- |
| `security` rows | **328** |
| `security_identity_event` rows | **450** |
| `universe_membership` rows | **328** |
| `evidence_event` rows | **0** |
| Overall posture | **ZERO_EVALUABLE** (identity present; zero evidence — honest) |
| Tiingo bootstrap usage | 0/450 (Tiingo key pending from owner) |

## Update — identity reconciliation (2026-08-28)

The live research.db identity layer was found **misaligned with the frozen
cohort**: the first bootstrap inserted the alphabetical head of
company_tickers.json (328 CIKs starting A/AA/AAAU), not the hash-selected
sample — only 16 of 443 cohort CIKs were present. Root cause: the bootstrap
invocation used a different ticker list than the frozen universe_sample.

Correction (append-only-safe, `reconcile_cohort_identity` in
`tradehub_research/backfill/security_bootstrap.py`):
- 427 missing cohort securities inserted; 6 mismatched canonical tickers
  corrected with superseding `ticker_change` identity events;
- 312 non-cohort memberships superseded to eligible=0 (they can never
  screen again; rows retained as the honest correction record);
- 427 cohort memberships added → 443 eligible terminal memberships, exactly
  matching the frozen cohort's 443 unique CIKs (450 tickers, share-class
  CIK dedup).
- Tiingo backfill then resumed against the corrected identity: the 440
  erroneous DUPLICATE_CIK ledger rows from the misaligned run remain
  visible append-only (an honest record of the defect).

## Update — real Tiingo/SEC backfill (2026-08-28, in progress)

1. **Tiingo API key** (owner-supplied) → bounded EOD backfill for the
   BOOTSTRAP_COHORT within the 450-symbol/30-day rolling ceiling
   (`tradehub_research/backfill/tiingo_driver.py` — designed, awaiting key).
2. **SEC bulk data** (companyfacts.zip / submissions.zip) for XBRL facts +
   Form 4 history (`tradehub_research/backfill/sec_driver.py` — designed).
3. Monthly PIT grid replay + outcome labels + baselines/ablations against
   the real ingested evidence (`validation/` engine is built and
   RA-05-verified; it consumes whatever the backfill produces).

Until (1)–(2) land, investment evidence is `INSUFFICIENT DATA` — a valid,
explicitly permitted Phase-5 verdict (handoff §18), not something to tune
away.
