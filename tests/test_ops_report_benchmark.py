"""The weekly report's Benchmark/Relative fields must actually be wired.

Live defect (found 2026-09-25): ``build_weekly_report`` accepted
``benchmark_pct`` but ``main()`` never supplied it and never computed one, so
"Benchmark:" and "Relative:" could NEVER render a number -- whatever the system
did. A permanently-dead field is indistinguishable from a quiet market, which is
exactly the failure this repo refuses elsewhere (missing values must be visibly
missing, with an error type, never a plausible-looking placeholder).

Wiring it exposes a second, deeper defect: the pinned Kenneth French artifact
records the cache path of the PRE-MIGRATION checkout
(``data/research/raw/benchmark/ff_daily_factors.csv``) -- a tree that no longer
exists. A correct resolver rebases that path onto the LIVE raw-cache root (the
cache file stays content-addressed and hash-verified, so a wrong file fails closed
rather than silently substituting).

And a third: the pinned vintage's own series ends 2026-06-30, so it cannot cover
a September report window at all. That is a DATA gap, not a wiring gap, and the
report must say so rather than rendering a bare "unavailable".
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from tradehub_research.validation.benchmark import (
    parse_ff_daily_factors,
    pin_benchmark_artifact,
    resolve_benchmark_cache_path,
    window_return_pct,
)
from tradehub_research.validation.experiment_db import ExperimentDB

FF_PREAMBLE = (
    "This file was created by using the 202606 CRSP database.\r\n"
    "The Tbill return is the simple daily rate.\r\n"
    "\r\n"
    ",Mkt-RF,SMB,HML,RF\r\n"
)
FF_FOOTER = "\r\nCopyright 2026 Eugene F. Fama and Kenneth R. French\r\n"


def _ff_text(rows: list[tuple[str, float, float, float, float]]) -> str:
    """Build Kenneth-French-shaped daily factor text for (date, mkt-rf, smb, hml, rf)."""
    body = "".join(
        f"{day.replace('-', '')}, {mkt:>7.2f}, {smb:>7.2f}, {hml:>7.2f}, {rf:>7.2f}\r\n"
        for day, mkt, smb, hml, rf in rows
    )
    return FF_PREAMBLE + body + FF_FOOTER


#: The live vintage shape: the daily file's last row is 2026-06-30.
LIVE_ROWS = [
    ("2026-06-26", 0.31, 0.05, -0.10, 0.01),
    ("2026-06-29", -0.42, 0.11, 0.20, 0.01),
    ("2026-06-30", 0.73, 0.10, -0.62, 0.01),
]
#: A vintage that does cover a September window.
SEPTEMBER_ROWS = [
    ("2026-09-17", 0.50, 0.10, -0.10, 0.01),
    ("2026-09-18", -0.25, 0.05, 0.05, 0.01),
    ("2026-09-21", 0.10, 0.00, 0.00, 0.01),
    ("2026-09-22", 0.30, 0.02, -0.02, 0.01),
    ("2026-09-23", -0.15, 0.01, 0.01, 0.01),
    ("2026-09-24", 0.40, 0.03, -0.03, 0.01),
]


@pytest.fixture()
def exp_db(tmp_path) -> ExperimentDB:
    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    return db


def _pin(exp_db: ExperimentDB, *, cache_path: str, text: str, vintage: str) -> str:
    """Pin an artifact exactly the way validation.pipeline does.

    ``fetch_and_pin_benchmark`` hashes the CSV as it reads it back in TEXT mode
    (universal-newline translation included), and ``load_benchmark_daily_returns``
    verifies against that same reading. The fixture must reproduce that contract or
    it tests a hash nobody else computes.
    """
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    as_read = path.read_text(encoding="utf-8", errors="replace")
    _, parsed_hash = parse_ff_daily_factors(as_read)
    return pin_benchmark_artifact(
        exp_db,
        source="ken-french-daily-factors",
        source_url="https://example.invalid/daily_factors.zip",
        vintage_label=vintage,
        raw_content_hash=hashlib.sha256(as_read.encode()).hexdigest(),
        parsed_series_hash=parsed_hash,
        cache_path=str(path),
    )


# ---------------------------------------------------------------------------
# window arithmetic
# ---------------------------------------------------------------------------
def test_window_return_compounds_the_sessions_inside_the_window():
    series = parse_ff_daily_factors(_ff_text(SEPTEMBER_ROWS))[0]
    pct = window_return_pct(series, "2026-09-17", "2026-09-24")
    # The base is the last session on/before the window start (2026-09-17), so the
    # window return is compounded over the sessions AFTER it -- the same period the
    # portfolio's own week measures (1,000,000 on 09-17 -> 1,010,000 on 09-24).
    rates = {day: (mkt + rf) / 100.0 for day, mkt, _smb, _hml, rf in SEPTEMBER_ROWS}
    inside = ("2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24")
    expected = (math.prod(1.0 + rates[day] for day in inside) - 1.0) * 100.0
    assert pct == pytest.approx(expected, abs=1e-9)
    assert pct != 0.0


def test_window_return_is_unavailable_when_the_vintage_never_reaches_the_window():
    """The live shape: a June vintage cannot price a September window."""
    series = parse_ff_daily_factors(_ff_text(LIVE_ROWS))[0]
    assert window_return_pct(series, "2026-09-17", "2026-09-24") is None


def test_window_return_is_unavailable_when_the_window_predates_the_series():
    series = parse_ff_daily_factors(_ff_text(SEPTEMBER_ROWS))[0]
    assert window_return_pct(series, "2026-01-05", "2026-01-30") is None


def test_window_return_is_unavailable_on_an_empty_series():
    assert window_return_pct({}, "2026-09-17", "2026-09-24") is None


# ---------------------------------------------------------------------------
# cache-path resolution (the pre-migration path recorded in the live artifact)
# ---------------------------------------------------------------------------
def test_stale_relative_cache_path_is_rebased_onto_the_live_raw_root(tmp_path):
    recorded = "data/research/raw/benchmark/ff_daily_factors.csv"
    raw_root = tmp_path / "var" / "lib" / "tradehub-research" / "raw"
    (raw_root / "benchmark").mkdir(parents=True)
    live = raw_root / "benchmark" / "ff_daily_factors.csv"
    live.write_text(_ff_text(SEPTEMBER_ROWS))

    # The recorded path is relative to a checkout that no longer exists.
    assert not Path(recorded).exists()
    assert resolve_benchmark_cache_path(recorded, raw_root) == live


def test_absolute_cache_path_that_still_exists_is_used_as_recorded(tmp_path):
    live = tmp_path / "benchmark" / "ff_daily_factors.csv"
    live.parent.mkdir(parents=True)
    live.write_text(_ff_text(SEPTEMBER_ROWS))
    assert resolve_benchmark_cache_path(str(live), tmp_path / "other") == live


def test_tampered_cache_file_fails_closed(tmp_path):
    from tradehub_research.validation.benchmark import load_latest_benchmark

    raw_root = tmp_path / "raw"
    live = raw_root / "benchmark" / "ff_daily_factors.csv"
    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    _pin(db, cache_path=str(live), text=_ff_text(SEPTEMBER_ROWS), vintage="v1")

    live.write_text(_ff_text(LIVE_ROWS), encoding="utf-8")  # substituted content
    with pytest.raises(ValueError, match="hash"):
        load_latest_benchmark(db, raw_root)


def test_load_latest_benchmark_returns_the_newest_vintage(tmp_path):
    from tradehub_research.validation.benchmark import load_latest_benchmark

    raw_root = tmp_path / "raw"
    (raw_root / "benchmark").mkdir(parents=True)
    old = raw_root / "benchmark" / "old.csv"
    new = raw_root / "benchmark" / "new.csv"
    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    _pin(db, cache_path=str(old), text=_ff_text(LIVE_ROWS), vintage="fetched 2026-07-01T00:00:00Z")
    _pin(
        db,
        cache_path=str(new),
        text=_ff_text(SEPTEMBER_ROWS),
        vintage="fetched 2026-09-25T00:00:00Z",
    )

    loaded = load_latest_benchmark(db, raw_root)
    assert loaded.vintage_label == "fetched 2026-09-25T00:00:00Z"
    assert loaded.last_session == "2026-09-24"


def test_load_latest_benchmark_raises_when_nothing_is_pinned(tmp_path):
    from tradehub_research.validation.benchmark import load_latest_benchmark

    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    with pytest.raises(ValueError):
        load_latest_benchmark(db, tmp_path / "raw")


# ---------------------------------------------------------------------------
# end to end: what the weekly report actually prints
# ---------------------------------------------------------------------------
HISTORY_ROWS = [
    {"date": "2026-09-17", "asset_value": 1_000_000.0, "deposits": None, "withdrawals": None},
    {"date": "2026-09-24", "asset_value": 1_010_000.0, "deposits": None, "withdrawals": None},
]
ANALYTICS = {"asset_value": 1_010_000.0, "cash_balance": 1_010_000.0}


def _weekly(tmp_path, monkeypatch, *, exp_db, settings):
    from tradehub_research.ops import report_cli

    monkeypatch.setattr(
        report_cli,
        "forward_health",
        lambda **kw: {"production_predictions": 0, "predictions_due": 0, "matured": {}},
    )
    monkeypatch.setattr(report_cli, "freshness_accounting", lambda **kw: _AUDIT_OK)
    monkeypatch.setattr(
        report_cli,
        "refresh_health",
        lambda **kw: {"securities_expected": 0, "with_bars": 0, "stale_count": 0},
    )
    return report_cli.build_weekly_report(
        settings=settings,
        experiment_db=exp_db,
        analytics=ANALYTICS,
        history=HISTORY_ROWS,
    )


_AUDIT_OK = {
    "universe": 443,
    "excluded_exceptions": 0,
    "lagging_within_window": 0,
    "unresolved": [],
    "unresolved_count": 0,
}


def _settings(tmp_path):
    from types import SimpleNamespace

    raw_root = tmp_path / "raw"
    (raw_root / "benchmark").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(busy_timeout_ms=5000, adapter_cache_dir=raw_root)


def test_weekly_report_renders_benchmark_and_relative_when_the_vintage_covers_the_window(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    live = settings.adapter_cache_dir / "benchmark" / "ff_daily_factors.csv"
    live.write_text(_ff_text(SEPTEMBER_ROWS))
    _pin(
        db,
        cache_path=str(live),
        text=_ff_text(SEPTEMBER_ROWS),
        vintage="fetched 2026-09-25T00:00:00Z",
    )

    report = _weekly(tmp_path, monkeypatch, exp_db=db, settings=settings)

    assert "Benchmark: +" in report, report
    assert "Relative: +" in report and "pp" in report
    assert "Benchmark: unavailable" not in report


def test_weekly_report_states_the_vintage_gap_instead_of_a_bare_unavailable(tmp_path, monkeypatch):
    """The live vintage (last row 2026-06-30) cannot cover a September window."""
    settings = _settings(tmp_path)
    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    live = settings.adapter_cache_dir / "benchmark" / "ff_daily_factors.csv"
    live.write_text(_ff_text(LIVE_ROWS))
    _pin(db, cache_path=str(live), text=_ff_text(LIVE_ROWS), vintage="fetched 2026-08-28T14:43:47Z")

    report = _weekly(tmp_path, monkeypatch, exp_db=db, settings=settings)

    assert "Benchmark: unavailable" in report
    # The operator must learn WHY, or the field reads as a quiet market.
    assert "2026-06-30" in report, report
    assert "2026-09-24" in report, report


def test_weekly_report_states_a_missing_cache_file_instead_of_a_bare_unavailable(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    live = settings.adapter_cache_dir / "benchmark" / "ff_daily_factors.csv"
    _pin(
        db,
        cache_path=str(live),
        text=_ff_text(SEPTEMBER_ROWS),
        vintage="fetched 2026-09-25T00:00:00Z",
    )
    live.unlink()  # pinned, but the cache file is gone from the live root

    report = _weekly(tmp_path, monkeypatch, exp_db=db, settings=settings)

    assert "Benchmark: unavailable" in report
    assert "benchmark" in report.lower()
    assert "unavailable" in report


def test_weekly_report_prefers_the_deployment_aware_raw_cache_path(tmp_path, monkeypatch):
    """The deployed report cron does not carry RESEARCH_ADAPTER_CACHE_DIR.

    `settings.adapter_cache_dir` therefore silently falls back to the in-repo
    default there, while `paths.raw_cache` (derived from TRADEHUB_RESEARCH_DIR /
    TRADEHUB_RAW_CACHE) IS passed through. Using the settings value made the
    DEPLOYED weekly report read "benchmark unavailable (ValueError)" -- the pinned
    artifact reported missing -- instead of the vintage-coverage reason that an
    operator shell with the full env rendered.
    """
    from types import SimpleNamespace

    from tradehub_research.ops import report_cli

    settings = _settings(tmp_path)  # its adapter_cache_dir points at an EMPTY decoy
    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    # Mirror the LIVE artifact exactly: a RELATIVE pre-migration cache path, so
    # resolution depends on which cache root the report chooses.
    live = tmp_path / "live_raw" / "benchmark" / "ff_daily_factors.csv"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(_ff_text(LIVE_ROWS), encoding="utf-8")
    as_read = live.read_text(encoding="utf-8", errors="replace")
    _, parsed_hash = parse_ff_daily_factors(as_read)
    pin_benchmark_artifact(
        db,
        source="ken-french-daily-factors",
        source_url="https://example.invalid/daily_factors.zip",
        vintage_label="fetched 2026-08-28T14:43:47Z",
        raw_content_hash=hashlib.sha256(as_read.encode()).hexdigest(),
        parsed_series_hash=parsed_hash,
        cache_path="data/research/raw/benchmark/ff_daily_factors.csv",
    )
    paths = SimpleNamespace(raw_cache=tmp_path / "live_raw")
    monkeypatch.setattr(report_cli, "forward_health", lambda **kw: {"production_predictions": 0})
    monkeypatch.setattr(report_cli, "freshness_accounting", lambda **kw: _AUDIT_OK)
    monkeypatch.setattr(report_cli, "refresh_health", lambda **kw: {})

    report = report_cli.build_weekly_report(
        settings=settings,
        experiment_db=db,
        paths=paths,
        analytics=ANALYTICS,
        history=HISTORY_ROWS,
    )
    assert "ValueError" not in report, report
    assert "2026-06-30" in report and "not covered" in report, report


def test_weekly_report_without_history_keeps_todays_behaviour(tmp_path, monkeypatch):
    """No broker history -> no window -> no new claim about the benchmark."""
    settings = _settings(tmp_path)
    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    from tradehub_research.ops import report_cli

    monkeypatch.setattr(report_cli, "forward_health", lambda **kw: {"production_predictions": 0})
    monkeypatch.setattr(report_cli, "freshness_accounting", lambda **kw: _AUDIT_OK)
    monkeypatch.setattr(
        report_cli,
        "refresh_health",
        lambda **kw: {"securities_expected": 0, "with_bars": 0, "stale_count": 0},
    )
    report = report_cli.build_weekly_report(
        settings=settings, experiment_db=db, analytics={}, history=[]
    )
    assert "Benchmark: unavailable" in report
    assert "unavailable" in report
