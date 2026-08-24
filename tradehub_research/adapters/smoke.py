from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

from tradehub_research.adapters.sec import SecAdapter


def run_sec(user_agent: str) -> dict[str, object]:
    """Exactly two bounded SEC requests: one settled index and one immutable accession."""
    day = date.today() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    with tempfile.TemporaryDirectory() as directory:
        adapter = SecAdapter(user_agent=user_agent, cache_dir=Path(directory), max_attempts=1)
        checks = (
            ("index", lambda: adapter.fetch_daily_index(day)),
            (
                "accession",
                lambda: adapter.fetch_accession(
                    "edgar/data/320193/000032019325000079/aapl-20250927.htm"
                ),
            ),
        )
        outcomes: dict[str, object] = {}
        for name, fetch in checks:
            try:
                response = fetch()
                outcomes[name] = {"http_status": response.status, "bytes": len(response.raw_bytes)}
            except Exception as exc:  # noqa: BLE001 - preserve both bounded probe results
                outcomes[name] = {"error": f"{type(exc).__name__}: {exc}"}
    passed = all("http_status" in value for value in outcomes.values())  # type: ignore[operator]
    return {
        "status": "PASS" if passed else "FAIL",
        "requests": 2,
        "index_date": day.isoformat(),
        **outcomes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="bounded live adapter smoke (never part of pytest)"
    )
    parser.add_argument("--sec-user-agent", default=os.getenv("SEC_USER_AGENT"))
    args = parser.parse_args(argv)
    if not args.sec_user_agent or not any(
        marker in args.sec_user_agent for marker in ("@", "https://")
    ):
        parser.error("--sec-user-agent must be descriptive and include a contact")
    result: dict[str, object] = {}
    try:
        result["sec"] = run_sec(args.sec_user_agent)
    except Exception as exc:  # noqa: BLE001 - smoke must report an honest bounded result
        result["sec"] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    if os.getenv("TIINGO_TOKEN"):
        result["tiingo"] = {
            "status": "SKIP",
            "reason": "bounded smoke intentionally requires an explicit licensed invocation",
        }
    else:
        result["tiingo"] = {"status": "SKIP", "reason": "token not configured"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["sec"].get("status") == "PASS" else 1  # type: ignore[union-attr]


if __name__ == "__main__":
    raise SystemExit(main())
