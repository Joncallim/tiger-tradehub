# Eligibility reason-code diagnostics (P2 operator observability)

Status: **design only — not implemented in PR #66**.

## Observed gap

The two genuine production portfolio observations record
`portfolio_state_observation.reason_codes_json = []` while
`signal_status = INELIGIBLE` and `final_status = NO_ACTION`.

That is **not** evidence the decision was wrong. Both candidates genuinely fail
the `watch-on-score-band` rule (DISCOVER → WATCH requires conviction ≥ 40,
data_quality ≥ 0.50, agreement ≥ 0.40, trajectory INITIAL/RISING/STABLE, sector
coverage SUPPORTED):

```text
CMBMF: conviction 5, data_quality 0.156  -> below band
DLR-PK: conviction 0, data_quality 0.035 -> below band
```

The engine simply does not persist *why* a normal (non-triggering) evaluation
returned INELIGIBLE. The operator can see the state but not the deterministic
rationale.

## Explicit non-goals

- Do not change policy values.
- Do not change transition semantics.
- Do not force a reason into the existing transition-ledger contract.
- Do not make diagnostics an input to any decision.

This is operator observability only, with no investment authority.

## Proposed design (for hostile review before implementation)

For each candidate evaluated as INELIGIBLE, emit a **diagnostic record** (not a
transition) naming which outgoing rules failed which fixed fields:

```text
conviction_below_min
data_quality_below_min
agreement_below_min
trajectory_not_allowed
sector_not_allowed
opportunity_below_min
opportunity_above_max
position_mismatch
no_outgoing_rule
```

Shape constraints:

- deterministic order (rule priority, then field name);
- bounded size, fixed vocabulary — no free text, no model output;
- written to a separate append-only diagnostics surface, leaving
  `committee_transition` and `portfolio_state_observation.reason_codes_json`
  semantics untouched;
- reconstructible from the same durable inputs the engine already reads, so a
  replay produces identical diagnostics.

## Acceptance for the follow-up

- deterministic and byte-stable across re-runs on the same inputs
- no change to any existing reason code, transition, score, or proposal
- production proof: the two existing INELIGIBLE observations explain as
  `conviction_below_min` + `data_quality_below_min`
