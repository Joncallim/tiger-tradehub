"""Deterministic separation of ACCEPTANCE rows from GENUINE production rows.

The production research database contains a small number of synthetic
acceptance pipelines written during deployment verification. They are durable
and append-only, so they can never be removed; instead every reporting,
learning, or evaluation surface must be able to EXCLUDE them deterministically.

A row is an acceptance row iff it is reachable from an acceptance pipeline run.
The single source of truth is the run-id prefix, declared once here.

Consumers that MUST exclude acceptance rows:
  - production portfolio performance
  - forward-learning / outcome maturation results
  - investment-evidence conclusions
  - adaptive training/evaluation inputs
  - operator genuine-production counts

WIRING STATUS (kept honest — do not overclaim):
  WIRED:
    - tradehub_research/ops/operator_status.py  (genuine counts + provenance split,
      and the latest decision never resolves to an acceptance run)
    - tradehub_research/ops/forward_capture.py   (acceptance runs are never
      captured into the production forward ledger, and the implicit
      "latest run" selection skips them)
  NOT YET WIRED (tracked as follow-up; do NOT assume protection):
    - portfolio performance reporting
    - investment-evidence conclusion surfaces
    - adaptive training/evaluation inputs
  Callers in those surfaces should adopt genuine_clause()/is_acceptance_run()
  before they are treated as protected.
"""

from __future__ import annotations

ACCEPTANCE_RUN_PREFIXES: tuple[str, ...] = ("pr66-acceptance-",)

# SQL fragment + params for filtering pipeline_run_id columns.
ACCEPTANCE_SQL = " OR ".join("pipeline_run_id LIKE ?" for _ in ACCEPTANCE_RUN_PREFIXES)
ACCEPTANCE_SQL_PARAMS: tuple[str, ...] = tuple(f"{prefix}%" for prefix in ACCEPTANCE_RUN_PREFIXES)

GENUINE_SQL = " AND ".join("pipeline_run_id NOT LIKE ?" for _ in ACCEPTANCE_RUN_PREFIXES)
GENUINE_SQL_PARAMS: tuple[str, ...] = ACCEPTANCE_SQL_PARAMS


def is_acceptance_run(pipeline_run_id: str | None) -> bool:
    """True when the run id is an explicitly labelled acceptance run."""
    if not pipeline_run_id:
        return True  # unattributable => never counted as genuine production
    return str(pipeline_run_id).startswith(ACCEPTANCE_RUN_PREFIXES)


def genuine_clause(column: str = "pipeline_run_id") -> tuple[str, tuple[str, ...]]:
    """SQL predicate for GENUINE production rows.

    Exactly complementary to ``acceptance_clause``: NULL and empty run ids are
    never genuine production (matching ``is_acceptance_run``), so they are
    excluded here as well as claimed there.
    """
    preds = [f"{column} NOT LIKE ?" for _ in ACCEPTANCE_RUN_PREFIXES]
    preds.append(f"{column} IS NOT NULL")
    preds.append(f"{column} != ''")
    return (" AND ".join(preds), tuple(f"{prefix}%" for prefix in ACCEPTANCE_RUN_PREFIXES))


def acceptance_clause(column: str = "pipeline_run_id") -> tuple[str, tuple[str, ...]]:
    """SQL predicate for acceptance rows.

    NULL-safe to match ``is_acceptance_run``: an unattributable row (NULL) is
    never genuine production, so it counts as acceptance rather than falling
    through SQL three-valued logic.
    """
    likes = " OR ".join(f"{column} LIKE ?" for _ in ACCEPTANCE_RUN_PREFIXES)
    return (
        f"({likes} OR {column} IS NULL OR {column} = '')",
        tuple(f"{prefix}%" for prefix in ACCEPTANCE_RUN_PREFIXES),
    )
