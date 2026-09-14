# PR #67 — Decouple deterministic scoring lineage from bounded committee context

Status: **design accepted, NOT implemented**. Deliberately excluded from PR #66
so a representation change cannot ride inside an already-large
security/runtime PR.

## Accepted diagnosis (production, run `bcb7359…f5ef8`)

```text
decision candidates:            34
committee packs buildable:       2
blocked:                        32
  PASSING_EVIDENCE_GT_256:       8
  STRUCTURED_ROW_GT_4096:        0
  FINAL_BODY_GT_160000:         24
  OTHER:                         0
momentum-lineage hypothesis:  CONFIRMED
```

Two measured classes, one root cause:

- **8 × `PASSING_EVIDENCE_GT_256`** — the momentum screen *passed* and its
  evidence set contains a median of **4,200** ids (max 4,257) against a bound
  of 256. Every other family contributes 5–6 ids.
- **24 × `FINAL_BODY_GT_160000`** — momentum did *not* pass, so passing evidence
  is small (5–6), yet the pack body still exceeds 160 KB because the momentum
  screen's `raw_features_json` is ≈ **2.34 MB** (measured on ALNT:
  2,343,451 bytes) and rides into the serialized body.

Momentum time-series lineage is being forced into the same physical object used
both for deterministic scoring and for LLM context.

## What must NOT be done

- Do not raise `MAX_EVIDENCE_ROWS` / `MAX_BODY_BYTES` blindly.
- Do not truncate or drop **passing scoring evidence**.
- Do not change the momentum Hunter.
- Do not change scoring.
- Do not send thousands of daily bars to models.

Verified reason truncation is dangerous: `semantic_screen_payload` includes
`raw_features` and feeds `semantic_screen_hash`
(`tradehub_research/committee/scoring.py:67-90`), and screen `evidence_ids`
drive `scored_evidence`/`scored_evidence_hash` (`:111-139`, `:178-203`). A naive
capacity fix would silently change score identity.

## Target architecture

```text
FULL DETERMINISTIC LINEAGE  ->  scorer      (complete evidence identity)
BOUNDED COMMITTEE VIEW      ->  LLM         (truthful aggregate + bounded evidence)
```

Both bind to the same frozen candidate/pipeline run.

### Model evidence honesty (binding)

Where the bounded view omits individual time-series observations, the model must
be told it is receiving a **deterministic aggregate**, must not claim to have
inspected omitted individual bars, and may cite only artifacts actually present
in the committee schema. The full deterministic lineage stays auditable outside
the LLM context. Synthetic citations for convenience are forbidden. If the
current assessment schema cannot represent this honestly, propose the smallest
schema extension and hostile-review it before implementation.

## Equivalence gate (non-negotiable)

For every candidate whose v1 pack currently builds, the new representation must
produce identical:

- family contributions
- base evidence
- confluence bonus
- penalties
- raw score
- conviction
- data quality
- scored evidence identity / `scored_evidence_hash`
- trajectory-relevant semantic identity

Distinguish **numerical/scoring equivalence** from **artifact identity**: a
versioned/rebase of pack or assessment artifact ids is acceptable, a changed
score is not. Trajectory semantics must be preserved explicitly. If any value
changes, the design is rejected and redesigned — do not create a new scoring
version to make a pack fit.

## Acceptance for PR #67

- all 34 decision candidates reach committee materialization
- golden equivalence suite green against currently-buildable candidates
- the 2 already-SCORED production candidates keep byte-identical scoring output
- explicit statement of what the bounded view omits and how the model is told
