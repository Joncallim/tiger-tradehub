from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from tradehub_research.acceptance.packs.ra00 import ASSERTIONS
from tradehub_research.acceptance.sanitize import sanitize
from tradehub_research.acceptance.schema import AssertionResult, RunResult, Status


def commit_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def run_pack(pack_id: str) -> RunResult:
    run_id = f"ra00-{uuid.uuid4().hex[:12]}"
    if pack_id != "RA-00":
        assertions = [AssertionResult("pack.lookup", Status.FAIL, "unknown pack")]
    else:
        assertions = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for assertion_id, function in ASSERTIONS:
                try:
                    function(root)
                    assertions.append(AssertionResult(assertion_id, Status.PASS, "ok"))
                except AssertionError as exc:
                    assertions.append(AssertionResult(assertion_id, Status.FAIL, str(exc)))
                except Exception as exc:  # noqa: BLE001 - qualification boundary
                    assertions.append(
                        AssertionResult(
                            assertion_id, Status.ESCALATE, f"{type(exc).__name__}: {exc}"
                        )
                    )
    if any(a.status == Status.ESCALATE for a in assertions):
        status = Status.ESCALATE
    elif any(a.status == Status.FAIL for a in assertions):
        status = Status.FAIL
    elif any(a.status == Status.BLOCKED for a in assertions):
        status = Status.BLOCKED
    else:
        status = Status.PASS
    return RunResult(run_id, status, assertions, commit_sha())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", nargs="?", default="RA-00")
    args = parser.parse_args(argv)
    result = run_pack(args.pack)
    print(json.dumps(sanitize(result.to_safe_dict()), indent=2))
    return 0 if result.status == Status.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
