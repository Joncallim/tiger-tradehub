# Phase 5 — Data-Sufficiency Audit (Packet A, initial pass)

Status: **audit findings, pre-implementation**. This supersedes the expected-coverage
table in `docs/phase5-validation-research-architecture-handoff.md` §4 with the actual
live state observed on `main @ c200639`.

## Method

Inspected the live `research.db` in this worktree (`data/research/research.db`) and the
sibling execution worktree (`/home/jon/tiger-tradehub/data/research/research.db`)
directly via `sqlite3`, checked adapter-level durable-quota state
(`tradehub_research/adapters/tiingo.py`'s `bootstrap_symbol` table), checked both
`.env` files for `TIINGO_TOKEN`/`RESEARCH_SEC_USER_AGENT`, and checked for any
cron/systemd ingestion job.

## Findings

| Check | Result |
| --- | --- |
| `research.db` row counts (security, evidence_event, universe_membership, screen_result, candidate, pipeline_run, score_snapshot, model_assessment, trade_proposal, experiment_run, oos_evaluation_log, sealed_holdout, snapshot_manifest) | **0 in every table, both worktrees** |
| `TIINGO_TOKEN` configured | **No**, in either `.env` |
| `RESEARCH_SEC_USER_AGENT` configured | **No**, in either `.env` |
| Tiingo adapter's own `bootstrap_symbol` rolling-30-day quota table | **Empty** — zero Tiingo API calls ever made, historical or live |
| SEC EDGAR adapter (`tradehub_research/adapters/sec.py`) | Exists (daily-index, companyfacts/XBRL, Form 4 parsers), **never invoked against live data** |
| Cron/systemd ingestion job | **None found** |

## Revised conclusion vs. handoff §4

The handoff's §4 table describes momentum/valuation/quality/inflection/informed-activity/event
as EVALUABLE, PARTIAL, or PARTIAL/sparse based on an assumed partial backfill. The live
audit found **zero ingested data of any kind** — every row in that table is presently
**ZERO-EVALUABLE**, not a coverage gap to route around. This is a complete absence of
ingested evidence, not partial coverage.

## Path forward (per owner authorization)

1. Build the validation ENGINE regardless — schema/`experiment.db` boundary, statistics,
   outcome builder, RA-05 methodological tests exercised via synthetic/fixture data
   (this does not require live data and proves the mechanism is trustworthy).
2. Separately, perform a **real bounded** Tiingo/SEC backfill once credentials are
   available (Tiingo API key pending from the owner; SEC User-Agent will be a generic
   placeholder contact string) so investment evidence can eventually be evaluated
   against real data rather than only synthetic fixtures.
3. Both efforts respect existing adapter guardrails (`NetworkClient` rate-limit/token-bucket/
   cache-budget machinery, the Tiingo 450-symbol/30-day rolling ceiling) without loosening
   them, and a hash-selected deterministic PIT-universe sample (frozen before any price
   retrieval, never selected on future outcome) if the full eligible universe exceeds the
   backfill entitlement, per handoff §4.1.

`INSUFFICIENT DATA` remains an explicitly valid Phase-5 investment-evidence verdict per
handoff §18 and is not something this epic tunes away.
