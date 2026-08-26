# Changelog

## 0.2.0 - Unreleased

- V2 Phase 3 (Epic #36): portfolio state machine & paper proposals.
  - New `tradehub_research/portfolio/` plane: canonical 7-state machine with
    derived current state over an immutable transition ledger; versioned POLICY
    contract (FIXTURE/PROVISIONAL/PAPER, fail closed, no hardcoded doctrine);
    score-driven eligibility that can never trade by itself; evidence-driven
    persistence/hysteresis; verified thesis-break bypass; SELL asymmetry with
    long-only guards; deterministic band sizing with cash/no-action first-class;
    PIT-correct volatility/correlation/ADV from the evidence ledger; restart-safe
    daily aggregate activity budget (count + notional) from the proposal ledger.
  - Typed immutable PAPER trade proposals with deterministic IDs and full lineage.
  - Terse deterministic M/W/F briefing; `No portfolio action recommended.` is a
    first-class output.
  - Schema migration 10: 13 append-only tables (policy, snapshot, observations,
    transitions, proposals, activity budget, thesis-break events, briefings).
  - CLI: `tradehub-research portfolio policy-register|run|replay|briefing`.
  - RA-03 acceptance pack: 26 deterministic assertions, all PASS; no-execution
    boundary enforced by an AST scan of the research plane.

## 0.1.0 - Unreleased

- Harden API bearer-token comparison, credential masking, and token strength validation.
- Make confirmation tokens atomic and retryable after transient live-placement failures.
- Require confirmation for every order submit path.
- Limit Release 1 order support to USD-denominated limit orders.
- Sanitize upstream broker errors returned to clients and stored in audit payloads.
- Add account-read Telegram commands and fail-closed Telegram chat authorization.
- Add CI, Dependabot, dependency audit, dependency lockfile, license, and packaging metadata.
- Expand tests for policy, authentication, audit token lifecycle, submit flows, and Tiger gateway
  wiring.
