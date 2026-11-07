# Acceptance data isolation

Issue #69 excludes synthetic `pr66-acceptance-*` pipeline descendants from
evaluation inputs. Historical rows and append-only triggers remain unchanged.

| Surface | Authority and exclusion |
| --- | --- |
| Portfolio P&L and returns | `ops/report_cli.py` reads broker analytics and broker history. `validation/reporting.py` normalizes/renders those inputs. Neither aggregates decision rows. |
| Operator investment/decision summary | `ops/operator_status.py` joins observations and scores to their pipeline ancestry and uses `genuine_clause()` for genuine counts and latest decision. |
| Investment evaluation conclusions | `validation/pipeline.py` obtains screens through `replay.load_screen_results()`. That loader filters pipeline ancestry before constructing outcome observations, baseline signals, ablations, walk-forward inputs and the sealed holdout. |
| Forward evidence | `ops/forward_capture.py` rejects acceptance runs and skips them during latest-run selection. `record_prediction_from_screen()` also rejects explicitly supplied acceptance/unattributable run IDs for production provenance. |
| Evaluation regime | `validation/regime.py` derives frozen windows from supplied coverage dates, not decision rows, scores or returns. |
| Adaptive learning | No production adaptive learner exists. The evaluation screen loader is protected; a future learner must use protected lineage-aware inputs. No weights or policy change here. |
| Coverage diagnostics and snapshots | Raw counts and frozen copies preserve all historical artifacts. They are storage/coverage diagnostics, not a clean training set or proof of investment performance. |

`tests/test_acceptance_consumers.py` mixes genuine and synthetic screen rows,
conflicting synthetic scores, and portfolio decision rows. It checks loader
exclusion, unchanged evaluation signals/conclusions and broker report values,
production-ingress rejection, retained history and append-only enforcement.

## Limits requiring separate verification

Old `forward_prediction` rows retain provenance but no pipeline-run foreign key.
This change cannot identify an already-captured synthetic prediction from its
prefix. Audit restored runtime records against capture artifacts before relying
on the existing production cohort. Do not relabel, delete or retrofit outcomes.
Low-level `record_prediction()` and legacy pure screen callers also have no
pipeline ancestry; their supplied provenance is a caller responsibility.

Cross-run production decision history (score trajectory and portfolio state,
persistence and prestate hashes) is separate from these reporting/evaluation
readers. Those paths have global historical reads. Their exposure to the actual
acceptance records is unverified without restored data; this PR does not claim
to isolate them or alter their state-machine semantics. A bounded reproduction
is needed before any further change under the observation-mode freeze.

## Separate assessment of #78

`forward_health()` still executes production due-set and outcome aggregation
queries. The experiment schema has no explicit provenance/due-date index;
`forward_outcome.prediction_id` already has a UNIQUE index, and prediction IDs
already have their primary-key index. Therefore the issue's join-index hypothesis
is not sufficient by itself. Check query plans and time the exact queries on a
copy of restored data before choosing an index or query rewrite. Any index must
use the schema migration path and preserve every output value and key.

An offline SQLite database built from the current experiment migrations confirms
that the due-set query scans `forward_prediction`, while its outcome existence
check uses the existing UNIQUE covering index. The horizon aggregation also
scans predictions and probes that outcome index. These are empty-schema query
plans, not a reproduction of the live 83-second timing or evidence of a speedup.

No #78 implementation is included. This avoids duplicating #87's maturation,
health accounting and restart work or any unverified Hermes changes.
