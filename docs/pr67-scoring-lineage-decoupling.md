# PR #67 — Decouple deterministic scoring lineage from bounded committee context

Status: **implemented** on `feat/pack-representation-decoupling` (base: merged
main `6a4154d`, i.e. post-#66). Design was accepted and deliberately excluded
from #66 so a representation change could not ride inside an already-large
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

Re-measured with an instrumented rebuild of the v1 body assembly (reproduces the
tally exactly: 8 / 24 / 2 buildable):

| class | n | mechanism |
|---|---|---|
| `PASSING_EVIDENCE_GT_256` | 8 | momentum *passed*; its evidence set is a median of 4,200 ids (max 4,257) against the 256-row bound. |
| `FINAL_BODY_GT_160000` | 24 | momentum did *not* pass, so passing evidence stays small — but the frozen bar set is 1,100–4,300 ids, and the 256-row interpretive slice alone serializes to **205–222 KB**, over the 160 KB body cap. |

Precision correction to the original note: the momentum screen's `raw_features_json`
is ≈2.34 MB raw, but `MAX_FEATURE_SOURCES` already bounds the serialized feature
projection to ≈28 KB; the term that actually overflows the body budget is the
**bar-level evidence rows** the series references. So the defect is not "one big
field" — it is that the single artifact must simultaneously carry complete
deterministic scoring identity and bounded interpretive context, under one byte
cap and one row cap.

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
(`tradehub_research/committee/scoring.py`), and screen `evidence_ids` drive
`scored_evidence`/`scored_evidence_hash`. A naive capacity fix would silently
change score identity.

## Implemented architecture

```text
FULL DETERMINISTIC LINEAGE  ->  scorer   (complete evidence identity, no model cap)
BOUNDED COMMITTEE VIEW      ->  LLM      (code-computed aggregates + bounded evidence)
```

Both bind to the same frozen candidate / pipeline run / `as_of` / screen results
and are built from **one shared PIT loader**
(`tradehub_research/committee/frozen_inputs.py`), so provenance, PIT, security,
screen-match, cluster and underlying-group gates cannot drift between the
artifact a scorer reads and the artifact a model sees.

### Artifacts

| artifact | storage | contents |
|---|---|---|
| `ScoringLineage` | `scoring_lineage` (append-only) | every frozen screen with its **complete** evidence identity, the versioned scoring-identity projection of `raw_features` (`SCORING_PROJECTION_VERSION = 1`, byte-identical to the v1 projection), and complete evidence identity for every frozen observation. |
| `CommitteeView` | `evidence_pack.pack_spec_version = 2` | bounded model-facing artifact: series features replaced by deterministic aggregates, bounded interpretive evidence, explicit omission metadata, `lineage_hash` reference. |
| pack v1 | `evidence_pack.pack_spec_version = 1` | unchanged, still the scoring input for pre-#67 runs and acceptance fixtures. |

`committee_run.pack_hash` pins the view; the run→lineage association lives in the
append-only `committee_run_lineage` mapping table (see migration/rollback below).

### Model evidence honesty (binding)

The view is self-describing and machine-checkable: `representation`,
`model_honesty.{citation_scope, reasoning_scope, aggregate_fields_are_code_computed,
series_omissions_are_aggregate_represented, interpretive_omissions_are_not_aggregated,
omission_counts_are_not_data_quality,
lineage_set_hash_is_an_identity_not_market_evidence}`,
`evidence_omitted.{interpretive_omitted, series_observations_omitted,
series_references_not_frozen, series_representatives_presented, semantics}`, and
per-screen `series_aggregates[]` with `observation_count`,
`observations_presented`, `observations_compacted`, `observations_not_frozen`,
session-date range and `lineage_set_hash`.

Two invariants hold by construction and are asserted by regressions: every
`representative_evidence_ids` entry an aggregate advertises (in the shipped
`raw_features` copy and in the per-screen summary alike) is a citable
`body["evidence"]` row, and every screen's `evidence_ids` is reconciled with what
was actually admitted. Observations a feature references but that are not this
candidate's frozen evidence are counted separately and carry no retention claim.

The model may cite only evidence rows present in the view (the assessment
firewall builds `in_pack` from `body["evidence"]`, so an omitted observation id is
refused). Observations folded into an aggregate are **not** re-serialized, so they
cannot crowd out interpretive evidence. Prompt contract: `prompt_version: "v2"`
(skill `tradehub-committee-worker-v2`); v1 remains the historical prompt.

### Committee work is pinned to one artifact

`committee_work.pack_hash` (existing column) is the pin. The MCP research tool
`get_evidence_pack(candidate_id, pack_hash=…)` returns exactly that artifact and
fails closed on a mismatch; **unpinned** lookups are refused while the candidate
has outstanding committee work, so a worker can never silently be shown a newer
artifact than the one its work was issued against. Newest-artifact races cannot
change what issued work sees.

### Migration / rollback

Migration 12 is **purely additive**: `CREATE TABLE scoring_lineage`,
`CREATE INDEX`, `CREATE TABLE committee_run_lineage`, append-only triggers. No
`ALTER`, no `DROP`, no `DELETE`, no `UPDATE`; `committee_run` keeps its historical
11-column shape. Regression: `tests/test_committee_identity_migration.py`.

Verified against a production copy migrated by the new code, then exercised with
the **pre-#67 (#66) code**:

```text
migrate()                         -> 12, no error
committee_run columns             -> unchanged (11)
legacy positional INSERT          -> ok
legacy committee run resume       -> same run id
router initialize + status        -> SCORED
legacy score snapshot reuse       -> conviction 5, scored_evidence_hash 400f9774…
check().integrity                 -> ok
check().ok                        -> false  (see caveat below)
```

Code rollback therefore does **not** require restoring the 4.6 GB database.

**Caveat (must be in the rollback runbook).** `check().ok` goes false purely
because the pre-#67 build compares `schema_version()` (12) against its own
compiled expectation (11). No committee operation fails, but this is not merely
cosmetic: `tradehub_research/cli.py` maps it to a non-zero **process exit code**,
so any deploy-verification or health tooling that gates on `research check` will
see a false failure after a rollback onto a migrated database. Do not use
`research check` as the rollback-success signal; use the operation-level checks
above instead.

Known residual: the pre-#67 build has no guard against scoring a *view* (that
guard is new code). If it were rolled back onto a database where #67 had already
created view-pinned runs, it would need those runs re-issued; restoring is not
required for correctness of existing (v1-pinned) history.

## Equivalence gate

Identical for every candidate whose v1 pack builds; measured on the two genuine
production candidates (frozen inputs unchanged):

| | CMBMF | DLR-PK |
|---|---|---|
| v1 pack hash (rebuilt == stored) | `91d8326d…6021` | `a80c2db4…f847` |
| family contributions, groups, penalties, base evidence, confluence | identical | identical |
| raw score | 6.04 | 0.0 |
| conviction | 5 | 0 |
| data quality | 0.156 | 0.034667 |
| `scored_evidence_hash` | `400f9774…` | `c47e4ec4…` |
| semantic screen hashes | identical | identical |
| persisted production snapshot | matches | matches |

Artifact identity (lineage 67,209 B vs pack 147,047 B for CMBMF) differs
legitimately; scoring identity does not. Legacy pack reproducibility is asserted
by rebuilding v1 through the refactored shared loader and comparing to the stored
row: identical `pack_hash` and byte-identical canonical body (also proven
cross-tree against main@`6a4154d` for a synthetic fixture, hash `ea586446…`).

Trajectory semantics: the prior run is resolved through the same artifact
resolver, so a representation change cannot manufacture
`SCREEN_METHODOLOGY_CHANGE`; unchanged scored evidence with a changed committee
representation remains `MODEL_REASSESSMENT`.

## Capacity (production copy, genuine run)

```text
decision candidates        34
materialized               34
PACK_TOO_LARGE              0
controls refused            5
view bytes   median 39,871 · p95 52,501 · max 52,530   (cap 160,000)
lineage bytes median 1.47 MB · max 2.36 MB            (~50 MB total)
interpretive evidence omitted 0 for every candidate
series observations omitted are counted + hashed + range-reported per feature
```

The 160 KB view cap is unchanged (no bound was relaxed to make this fit).

## Acceptance for PR #67

- [x] all 34 decision candidates reach committee materialization
- [x] golden equivalence suite green against currently-buildable candidates
- [x] the 2 already-SCORED production candidates keep identical scoring output
- [x] explicit statement of what the bounded view omits and how the model is told
- [x] migration is rollback-safe; v1 artifact identity reproduced
- [x] committee work pinned to an exact artifact, fail-closed

## Review rounds — independent adversarial review of the complete architecture

| round | verdict | findings | disposition |
|---|---|---|---|
| 1 | REJECT | P1 methodology identity inherited the model-facing row cap; P2 exact-pin enforcement; P2 overstated rollback claim; P3 shared omission label | all fixed (`c460124`) |
| 2 | REJECT | P1 scoring cap aliased to the view cap; P2 small series serialized raw; P2 run→lineage mapping trusted the caller; P3 pre-work window | all fixed (`259ec2a`) |
| 3 | REJECT | P1 an aggregate could advertise a representative that is never presented | fixed (`ef3ef5b`) |
| 4 | REJECT | **P1** the trim never reached the shipped `raw_features` copy (`truncate_strings` rebuilds the tree); **P2** screens' `evidence_ids` unreconciled with admission; **P2** confluence groups derived over all frozen rows while pack v1 derived over its selection, so a merge caused by rows outside it could move `scored_evidence_hash`; P3 foreign lineage accepted; P3 doc/skill field-name drift; P3 series counts conflated "not frozen" with "compacted"; P3 probe rows double-recorded truncation receipts | all fixed (`ef3ef5b` → this commit) |

## First integrated review — REJECT, and disposition

The first integrated adversarial review (frontier gate, complete-architecture
prompt, read-only) returned **REJECT** with 1×P1, 2×P2, 1×P3. Every finding was
reproduced and is genuine; all four are remediated here:

| finding | disposition |
|---|---|
| **P1** `semantic_screen_hash` inherited the 256-row pack cap through screen `evidence_ids`, while the lineage keeps the complete set — so a legacy SCORED candidate with >256 frozen ids and ≤256 passing ids could be re-classified `SCREEN_METHODOLOGY_CHANGE` from a pure representation change. | Fixed: `scoring.methodology_evidence_projection` applies the *historical* passing-first/id-capped projection before hashing, from either artifact, and is idempotent on a legacy pack. Versioned as `SEMANTIC_EVIDENCE_PROJECTION_VERSION`, documented as scoring identity — never a view bound. New regression `tests/test_committee_methodology_projection.py` builds exactly the 300-frozen/20-passing boundary and asserts identity equality plus `MODEL_REASSESSMENT` end-to-end. |
| **P2** the pinned `get_evidence_pack` lookup accepted any existing artifact for the candidate, not the candidate's outstanding work pin. | Fixed: while outstanding committee work exists, a pin must equal one of those work pins or the lookup fails closed before any model spend. Regression extended (different existing artifact refused; own pin resolves once that run's work is issued). |
| **P2** the rollback claim understated that `research check` exits non-zero. | Fixed: caveat added above; rollback runbook must not gate on `research check`. |
| **P3** one `omission_semantics` label covered both aggregate-represented series omissions and capacity-dropped interpretive rows. | Fixed: `evidence_omitted.semantics` now separates `series_observations` (aggregate-represented, lineage-retained) from `interpretive_rows` (capacity-bound, not presented and not aggregated), with an explicit statement that neither is a data-quality signal. Regression asserts the labels differ. |
