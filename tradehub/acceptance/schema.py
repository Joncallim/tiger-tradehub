"""Result schema for the functional acceptance runner.

The schema is versioned and deliberately minimal. Every pack produces
exactly one RunResult. Public artifacts must never contain secrets;
sanitisation is applied by the runner before serialisation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    ESCALATE = "ESCALATE"


class AssertionResult(BaseModel):
    """One deterministic assertion outcome within a pack."""

    id: str
    status: Status
    detail: str = ""


class RunResult(BaseModel):
    schema_version: int = 1
    pack_id: str
    run_id: str
    environment: str
    status: Status
    commit_sha: str
    started_at: datetime
    finished_at: datetime
    assertions: list[AssertionResult] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    safe_summary: str = ""
    escalation_reason: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def aggregate_status(assertions: list[AssertionResult]) -> Status:
    """Deterministic pack-level aggregation.

    ESCALATE wins over FAIL; FAIL wins over BLOCKED. This ordering is
    fixed so the agent can never reinterpret per-assertion outcomes.
    """
    if any(a.status == Status.ESCALATE for a in assertions):
        return Status.ESCALATE
    if any(a.status == Status.FAIL for a in assertions):
        return Status.FAIL
    if any(a.status == Status.BLOCKED for a in assertions):
        return Status.BLOCKED
    return Status.PASS
