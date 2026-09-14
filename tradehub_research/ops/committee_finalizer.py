"""Asynchronous committee finalizer for the persisted PAPER decision path.

This process has no model client, broker client, or prompt surface. It only
observes persisted committee submissions, creates a score after READY_TO_SCORE,
then invokes the existing deterministic portfolio engine/exporter. All missing
or malformed state is returned as a fail-closed no-action result.
"""

from __future__ import annotations

import json

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.ops.common import research_paths
from tradehub_research.ops.decision_pipeline import finalize_async_committee_decisions


def main() -> int:
    settings = ResearchSettings()
    paths = research_paths()
    database = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    summary = finalize_async_committee_decisions(database)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
