# FORWARD OBSERVATION MODE (2026-08-31, owner brief)

TradeHub has reached autonomous PAPER operations. Until the forward-evidence
gate is met, the deployed system runs UNCHANGED. This file is the standing
record of the freeze and the observation contract.

## CHANGE FREEZE (until the gate)

Do NOT change:

- Hunter definitions / thresholds
- scoring weights
- candidate thresholds
- portfolio-state thresholds
- committee structure
- model routing based on investment results
- investment horizon
- risk constitution
- PAPER autonomy policy except for genuine safety defects

PERMITTED: dependency/security/operational fixes (missed timer, duplicate
run, failed ingestion, stale-data detection, reconciliation bug, incorrect
arithmetic, broken report, security issue, broker/API incompatibility).

Any change that could alter investment decisions MUST create a new version
and be explicitly recorded here. Do NOT disguise strategy tuning as a bug
fix. Potential improvements go to the improvement log (below), not
production.

## ADAPTATION GATE (#41 remains OPEN, UNSTARTED)

- 21-session outcomes  -> early diagnostic evidence only
- 63-session outcomes  -> first useful strategy-quality review
- 126-session outcomes -> serious adaptive-layer design may be reconsidered
- 252-session outcomes -> strongest first full-cycle evidence

Sample size and effective-N matter more than calendar date. If samples
remain sparse/correlated: WAIT LONGER. Do not open #41 implementation work
merely because 21-session outcomes start arriving. #41 requires explicit
owner authorization (an ADAPTIVE READINESS REVIEW first).

## IMPROVEMENT LOG (recorded, NOT applied)

- (empty)
- (2026-09-01) [RESOLVED] delisted 200-empty symbols (TALMF) are now retired automatically (retired_securities.json) and excluded from refresh + staleness.

## EVIDENCE BASELINE (entering observation mode)

- Forward predictions: 10,632 production (as_of 2026-08-28, 2,658 screens x 4 horizons)
- replay_bootstrap rows: 308,328 (permanently excluded from production evidence)
- Matured outcomes: 0 (first 21-session maturations due ~2026-10-01 SGT)
- PAPER executions: 0 real (dry-run only; TRADEHUB_DRY_RUN=true)
- Benchmark: FF factors pinned, vintage ends 2026-06-30 (benchmark returns
  for the current period remain unavailable until refreshed)
