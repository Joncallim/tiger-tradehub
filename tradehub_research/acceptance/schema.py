from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    ESCALATE = "ESCALATE"


@dataclass
class AssertionResult:
    id: str
    status: Status
    detail: str = ""


@dataclass
class RunResult:
    run_id: str
    status: Status
    assertions: list[AssertionResult]
    commit_sha: str
    pack_id: str
    schema_version: int = 1
    artifacts: list[str] = field(default_factory=list)

    def to_safe_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        for assertion in value["assertions"]:
            assertion["status"] = assertion["status"].value
        return value
