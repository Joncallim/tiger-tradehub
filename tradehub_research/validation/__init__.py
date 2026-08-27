"""TradeHub V2 Phase 5 -- validation engine package.

Historical/backtest evaluation over immutable point-in-time snapshots, with
results and governance stored in a separate ``experiment.db`` -- never
mutating the live ``research.db``. See ``docs/phase5-validation-research-architecture-handoff.md``
for the full design.
"""

from __future__ import annotations
