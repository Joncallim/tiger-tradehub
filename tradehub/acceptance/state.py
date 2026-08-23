"""Record run state so downstream packs can gate on upstream lineage.

State is written by the runner after every pack run and read by FA-05's
lineage gate. Only sanitized fields (pack id, status, commit, run id)
are persisted — never credentials or tokens.
"""

from __future__ import annotations

import json
from typing import Any

from tradehub.acceptance.runner import ARTIFACT_DIR
from tradehub.acceptance.schema import RunResult

STATE_FILE = ARTIFACT_DIR / "state.json"


OFFICIAL_PACKS = {"FA-00", "FA-01", "FA-02", "FA-03", "FA-04", "FA-05"}


def record_run(result: RunResult) -> None:
    # Only official acceptance packs are lineage-relevant; test-injected
    # pack IDs (e.g. hang-regression fixtures) must never pollute state.
    if result.pack_id not in OFFICIAL_PACKS:
        return
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            state = {}
    state[result.pack_id] = {
        "status": result.status.value,
        "commit_sha": result.commit_sha,
        "run_id": result.run_id,
        "finished_at": result.finished_at.isoformat(),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}
