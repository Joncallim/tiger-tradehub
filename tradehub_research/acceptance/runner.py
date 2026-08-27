from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from tradehub_research.acceptance.packs.ra00 import ASSERTIONS as RA00_ASSERTIONS
from tradehub_research.acceptance.packs.ra01 import ASSERTIONS as RA01_ASSERTIONS
from tradehub_research.acceptance.packs.ra02 import ASSERTIONS as RA02_ASSERTIONS
from tradehub_research.acceptance.packs.ra03 import ASSERTIONS as RA03_ASSERTIONS
from tradehub_research.acceptance.packs.ra04 import ASSERTIONS as RA04_ASSERTIONS
from tradehub_research.acceptance.packs.ra05 import ASSERTIONS as RA05_ASSERTIONS
from tradehub_research.acceptance.sanitize import sanitize
from tradehub_research.acceptance.schema import AssertionResult, RunResult, Status

PackAssertion = tuple[str, Callable[[Path], None]]

# Pack registration is an explicit whitelist: import each known pack above and add its
# assertions here. Never resolve a user-supplied pack ID through dynamic imports.
PACK_REGISTRY: dict[str, list[PackAssertion]] = {
    "RA-00": RA00_ASSERTIONS,
    "RA-01": RA01_ASSERTIONS,
    "RA-02": RA02_ASSERTIONS,
    "RA-03": RA03_ASSERTIONS,
    "RA-04": RA04_ASSERTIONS,
    "RA-05": RA05_ASSERTIONS,
}


def commit_sha() -> str:
    try:
        from tradehub_research._commit import __commit_sha__

        return __commit_sha__
    except ImportError:
        pass
    repository_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def run_pack(pack_id: str) -> RunResult:
    run_id = f"{pack_id.lower().replace('-', '')}-{uuid.uuid4().hex[:12]}"
    pack_assertions = PACK_REGISTRY.get(pack_id)
    if pack_assertions is None:
        assertions = [AssertionResult("pack.lookup", Status.FAIL, "unknown pack")]
    else:
        assertions = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for assertion_id, function in pack_assertions:
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
    return RunResult(run_id, status, assertions, commit_sha(), pack_id)


def main(argv: list[str] | None = None) -> int:
    """Run a registered pack, e.g. ``tradehub-research-acceptance RA-00``."""
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", nargs="?", default="RA-00")
    args = parser.parse_args(argv)
    result = run_pack(args.pack)
    print(json.dumps(sanitize(result.to_safe_dict()), indent=2))
    return 0 if result.status == Status.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
