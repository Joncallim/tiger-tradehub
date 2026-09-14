"""Exhaustive ``operator_status`` fault-injection matrix (final #66 review).

The recurring defect class this PR kept re-discovering was:

    an operation FAILED, but the payload rendered as though the system
    legitimately contained zero / nothing.

Instead of patching instances one at a time, every potentially failing
operation in ``operator_status.py`` is enumerated here and pinned to exactly
one of two outcomes:

  * FAILURE (unreadable path, broken query, unusable payload)
        -> an explicit UNKNOWN (``None`` / ``None``-bearing status) PLUS a
           recorded ``decision_chain["query_errors"][<operation>]`` entry;
  * ABSENCE (the documented, legitimate empty case)
        -> the documented zero / empty value with NO error recorded.

Any operation that turns a failure into a valid-looking empty state fails here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from tradehub_research.ops import operator_status as ops

GENUINE_RUN = "bcb73591996077de45a9c5e6ea8d72be2dd3781d870e999dbd9809bde83f5ef8"

TABLES = (
    "CREATE TABLE portfolio_run (run_id TEXT, pipeline_run_id TEXT, "
    "decision_as_of TEXT, created_at TEXT)",
    "CREATE TABLE portfolio_state_observation (decision_id TEXT, run_id TEXT)",
    "CREATE TABLE committee_run (committee_run_id TEXT, pipeline_run_id TEXT)",
    "CREATE TABLE score_snapshot (snapshot_id TEXT, committee_run_id TEXT)",
    "CREATE TABLE trade_proposal (proposal_id TEXT, decision_id TEXT)",
)

_ABSENT = object()

DEFAULT_FORWARD = {"production_predictions": 0, "predictions_due": 0, "matured": {}}
DEFAULT_REFRESH = {"securities_expected": 0, "with_bars": 0, "stale_count": 0}


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
def _research_db(tmp_path, *, rows=(), fail_on=()):
    """A REAL sqlite research DB behind a fake ``ResearchDB`` factory.

    ``fail_on`` holds SQL substrings; any statement containing one raises
    ``OperationalError``. That is how each individual query failure is
    fault-injected in isolation, with every other query left healthy.
    """
    con = sqlite3.connect(tmp_path / "research.db")
    con.row_factory = sqlite3.Row
    for ddl in TABLES:
        con.execute(ddl)
    for sql, values in rows:
        con.executemany(sql, values)
    con.commit()

    class _Conn:
        def execute(self, sql, params=()):
            for needle in fail_on:
                if needle in sql:
                    raise sqlite3.OperationalError(f"simulated failure on {needle!r}")
            return con.execute(sql, params)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _DB:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            return _Conn()

    return _DB


class _EmptyCursor:
    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _EmptyConn:
    def execute(self, sql, params=()):
        return _EmptyCursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ExperimentDB:
    def connect(self, *args, **kwargs):
        return _EmptyConn()


class _BoomExperimentDB:
    def connect(self, *args, **kwargs):
        raise sqlite3.OperationalError("simulated: experiment DB unavailable")


def _health(value):
    def _call(**kwargs):
        if isinstance(value, BaseException):
            raise value
        return value

    return _call


def _status(
    tmp_path,
    monkeypatch,
    *,
    db=None,
    cycle_log=_ABSENT,
    authority_dir=None,
    receipts=None,
    forward=DEFAULT_FORWARD,
    refresh=DEFAULT_REFRESH,
    experiment_db=None,
):
    monkeypatch.setattr(ops, "ResearchDB", db or _research_db(tmp_path))
    monkeypatch.setattr(ops, "forward_health", _health(forward))
    monkeypatch.setattr(ops, "refresh_health", _health(refresh))
    monkeypatch.setattr(
        ops, "RUNNER_RECEIPTS", receipts if receipts is not None else tmp_path / "ledger.jsonl"
    )
    if authority_dir is not None:
        monkeypatch.setattr(ops, "DEFAULT_AUTHORITY_DIR", authority_dir)
    if cycle_log is not _ABSENT:
        (tmp_path / "cycle-log.jsonl").write_text(cycle_log)
    paths = SimpleNamespace(
        research_db=tmp_path / "research.db",
        experiment_db=tmp_path / "experiment.db",
        research_dir=tmp_path,
    )
    return ops.operator_status(
        settings=SimpleNamespace(busy_timeout_ms=5000),
        experiment_db=experiment_db or _ExperimentDB(),
        paths=paths,
    )


def _all_field_names(payload) -> set[str]:
    """Every dict key anywhere in the payload."""
    found: set[str] = set()
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.update(str(key) for key in node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


_DIR_MODE = stat.S_IFDIR | 0o750


class _UnstattableDir:
    """The path (or one of its parent components) cannot be stat'd: EACCES."""

    def stat(self):
        raise PermissionError("EACCES: simulated stat failure on the authority dir")

    def iterdir(self):  # pragma: no cover - never reached when stat fails
        raise AssertionError("iterdir must never be reached when stat fails")


class _UnlistableDir:
    """The directory stats fine but cannot be listed (iterdir raises EACCES).

    Mirrors the real failure: ``Path.glob`` SWALLOWS this on CPython <= 3.12, so
    the code under test must not depend on glob to surface it.
    """

    def stat(self):
        return SimpleNamespace(st_mode=_DIR_MODE)

    def iterdir(self):
        raise PermissionError("EACCES: simulated unlistable authority directory")


class _NotADirectory:
    """A regular file where the authority DIRECTORY is expected."""

    def stat(self):
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o640)

    def iterdir(self):  # pragma: no cover - never reached
        raise AssertionError("iterdir must never be reached for a non-directory")


def _authority_dir(tmp_path, names=()):
    directory = tmp_path / "authority"
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / f"{name}.json").write_text("{}")
    return directory


# --------------------------------------------------------------------------
# 1. authority store read failures -> UNKNOWN, never an empty set
# --------------------------------------------------------------------------
def test_authority_directory_absent_is_a_documented_known_empty(tmp_path, monkeypatch):
    out = _status(tmp_path, monkeypatch, authority_dir=tmp_path / "does-not-exist")
    chain = out["decision_chain"]
    assert chain["authority_records_total"] == 0
    assert "authority_records_total" not in chain["query_errors"], chain["query_errors"]
    assert chain["latest_decision"]["eligible_exports"] == 0


def test_authority_directory_present_and_empty_is_a_known_zero(tmp_path, monkeypatch):
    empty = _authority_dir(tmp_path)
    monkeypatch.setattr(ops, "DEFAULT_AUTHORITY_DIR", empty)
    assert ops._published_authority_ids() == set()
    assert ops._authority_record_count() == 0
    out = _status(tmp_path, monkeypatch, authority_dir=empty)
    chain = out["decision_chain"]
    assert chain["authority_records_total"] == 0
    assert "authority_records_total" not in chain["query_errors"]


def test_authority_stat_failure_raises_and_is_reported_as_unknown(tmp_path, monkeypatch):
    """EACCES must NOT become 'zero authority records'."""
    monkeypatch.setattr(ops, "DEFAULT_AUTHORITY_DIR", _UnstattableDir())
    with pytest.raises(PermissionError):
        ops._published_authority_ids()

    out = _status(tmp_path, monkeypatch, authority_dir=_UnstattableDir())
    chain = out["decision_chain"]
    assert chain["authority_records_total"] is None, "EACCES must not read as 0"
    assert chain["query_errors"]["authority_records_total"].startswith("PermissionError")


def test_authority_listing_failure_is_reported_as_unknown(tmp_path, monkeypatch):
    """A directory that exists but cannot be listed is UNKNOWN, not empty."""
    with pytest.raises(PermissionError):
        monkeypatch.setattr(ops, "DEFAULT_AUTHORITY_DIR", _UnlistableDir())
        ops._published_authority_ids()

    out = _status(tmp_path, monkeypatch, authority_dir=_UnlistableDir())
    chain = out["decision_chain"]
    assert chain["authority_records_total"] is None
    assert chain["query_errors"]["authority_records_total"].startswith("PermissionError")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory mode bits")
def test_authority_unreadable_real_directory_is_unknown(tmp_path, monkeypatch):
    """The same class of failure, on a REAL unreadable directory."""
    directory = _authority_dir(tmp_path, names=("prop-a",))
    monkeypatch.setattr(ops, "DEFAULT_AUTHORITY_DIR", directory)
    directory.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            ops._published_authority_ids()
    finally:
        directory.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory mode bits")
def test_authority_with_an_unsearchable_parent_is_unknown_not_absent(tmp_path, monkeypatch):
    """The real production shape: an ancestor the reader cannot search.

    ``Path.exists()``/``is_dir()`` are NOT a reliable "is it absent?" test under
    EACCES -- their behaviour differs across CPython versions (they may return
    False or raise) -- which is exactly why the code under test uses an explicit
    stat() and treats only FileNotFoundError as absence.
    """
    locked = tmp_path / "locked"
    (locked / "authority").mkdir(parents=True)
    monkeypatch.setattr(ops, "DEFAULT_AUTHORITY_DIR", locked / "authority")
    locked.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            ops._published_authority_ids()
    finally:
        locked.chmod(0o700)


def test_authority_store_that_is_not_a_directory_is_unknown_not_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "DEFAULT_AUTHORITY_DIR", _NotADirectory())
    with pytest.raises(NotADirectoryError):
        ops._published_authority_ids()

    out = _status(tmp_path, monkeypatch, authority_dir=_NotADirectory())
    chain = out["decision_chain"]
    assert chain["authority_records_total"] is None
    assert chain["query_errors"]["authority_records_total"].startswith("NotADirectoryError")


def test_exports_unknown_while_the_proposal_count_stays_known(tmp_path, monkeypatch):
    """An unreadable authority store must not report 'this decision exported nothing'."""
    db = _research_db(
        tmp_path,
        rows=(
            (
                "INSERT INTO portfolio_run VALUES (?,?,?,?)",
                [("run-1", GENUINE_RUN, "2026-09-10T20:15:00Z", "2026-09-14T01:59:34Z")],
            ),
            (
                "INSERT INTO portfolio_state_observation VALUES (?,?)",
                [("d1", "run-1"), ("d2", "run-1")],
            ),
            (
                "INSERT INTO trade_proposal VALUES (?,?)",
                [("prop-a", "d1"), ("prop-b", "d2")],
            ),
        ),
    )
    out = _status(tmp_path, monkeypatch, db=db, authority_dir=_UnstattableDir())
    latest = out["decision_chain"]["latest_decision"]
    assert latest["run_id"] == "run-1"
    assert latest["proposals"] == 2, "a known fact must stay known"
    assert latest["eligible_exports"] is None, "UNKNOWN, not a fake 0"
    assert latest["exported_proposal_ids"] is None
    errors = out["decision_chain"]["query_errors"]
    assert errors["eligible_exports"].startswith("authority_store:PermissionError"), errors
    # The suite's global count is unknown too, and says so.
    assert out["decision_chain"]["authority_records_total"] is None


def test_authority_exports_intersect_when_the_store_is_readable(tmp_path, monkeypatch):
    """Control: a readable store still produces the real intersection."""
    db = _research_db(
        tmp_path,
        rows=(
            (
                "INSERT INTO portfolio_run VALUES (?,?,?,?)",
                [("run-1", GENUINE_RUN, "2026-09-10T20:15:00Z", "2026-09-14T01:59:34Z")],
            ),
            ("INSERT INTO portfolio_state_observation VALUES (?,?)", [("d1", "run-1")]),
            ("INSERT INTO trade_proposal VALUES (?,?)", [("prop-a", "d1"), ("prop-b", "d1")]),
        ),
    )
    directory = _authority_dir(tmp_path, names=("prop-a",))
    out = _status(tmp_path, monkeypatch, db=db, authority_dir=directory)
    latest = out["decision_chain"]["latest_decision"]
    assert latest["proposals"] == 2
    assert latest["eligible_exports"] == 1
    assert latest["exported_proposal_ids"] == ["prop-a"]
    assert out["decision_chain"]["authority_records_total"] == 1


# --------------------------------------------------------------------------
# 2. cycle-log failures -> recorded, never 'no cycle has ever run'
# --------------------------------------------------------------------------
def test_cycle_log_absent_is_a_documented_absence(tmp_path, monkeypatch):
    out = _status(tmp_path, monkeypatch)
    assert out["pipeline_status"] == {"last_cycle": None}
    assert "cycle_log" not in out["decision_chain"]["query_errors"]
    assert out["portfolio_status"]["last_cycle_decision_status"] is None


def test_cycle_log_that_is_a_directory_is_recorded_not_fatal(tmp_path, monkeypatch):
    """A read failure (EISDIR) must be distinguishable from an absent log."""
    (tmp_path / "cycle-log.jsonl").mkdir()
    out = _status(tmp_path, monkeypatch)
    assert out["pipeline_status"] == {"last_cycle": None}
    assert "cycle_log" in out["decision_chain"]["query_errors"]
    assert out["decision_chain"]["query_errors"]["cycle_log"].startswith("IsADirectoryError")


@pytest.mark.parametrize("body", ["[1, 2, 3]\n", '"a string"\n', "123\n", "null\n"])
def test_cycle_log_non_object_json_is_recorded_not_fatal(tmp_path, monkeypatch, body):
    """Valid JSON that is not an object must not crash on ``.get``."""
    out = _status(tmp_path, monkeypatch, cycle_log=body)
    errors = out["decision_chain"]["query_errors"]
    assert "cycle_log" in errors, errors
    assert "not an object" in errors["cycle_log"]
    assert out["portfolio_status"]["last_cycle_decision_status"] is None
    assert out["portfolio_status"]["last_run"] is None


def test_cycle_log_corrupt_decision_field_is_recorded(tmp_path, monkeypatch):
    """A non-object ``decision`` is recorded, not silently reported as None status."""
    out = _status(
        tmp_path,
        monkeypatch,
        cycle_log=json.dumps({"as_of": "2026-09-14", "decision": "BLOCKED_NO_VALID_SCORE"}) + "\n",
    )
    errors = out["decision_chain"]["query_errors"]
    assert "cycle_log_decision" in errors, errors
    assert out["portfolio_status"]["last_cycle_decision_status"] is None


def test_cycle_log_stat_failure_is_recorded_not_an_absence(tmp_path):
    """A stat failure on the log is an UNKNOWN, never 'no cycle has ever run'."""

    class _UnstattableLog:
        def stat(self):
            raise PermissionError("EACCES")

        def read_text(self, *args, **kwargs):  # pragma: no cover - never reached
            raise AssertionError("must not read after a stat failure")

    errors: dict[str, str] = {}
    assert ops._cycle_log_snapshot(_UnstattableLog(), errors) is None
    assert errors["cycle_log"].startswith("PermissionError")


def test_cycle_log_snapshot_reads_a_healthy_entry(tmp_path):
    log = tmp_path / "cycle-log.jsonl"
    log.write_text(json.dumps({"as_of": "2026-09-14", "decision": {"status": "OK"}}) + "\n")
    errors: dict[str, str] = {}
    entry = ops._cycle_log_snapshot(log, errors)
    assert entry is not None and entry["as_of"] == "2026-09-14"
    assert errors == {}


# --------------------------------------------------------------------------
# 3. decision-ledger query failures -> None + a recorded error, one by one
# --------------------------------------------------------------------------
LEDGER_QUERIES = (
    ("SELECT count(*) FROM portfolio_run", "portfolio_runs_total"),
    ("SELECT count(*) FROM portfolio_state_observation", "observations_total"),
    ("SELECT run_id, pipeline_run_id", "latest_decision"),
)


@pytest.mark.parametrize(("needle", "error_key"), LEDGER_QUERIES)
def test_each_decision_ledger_query_failure_is_unknown(tmp_path, monkeypatch, needle, error_key):
    db = _research_db(
        tmp_path,
        rows=(
            (
                "INSERT INTO portfolio_run VALUES (?,?,?,?)",
                [("run-1", GENUINE_RUN, "2026-09-10T20:15:00Z", "2026-09-14T01:59:34Z")],
            ),
            ("INSERT INTO portfolio_state_observation VALUES (?,?)", [("d1", "run-1")]),
        ),
        fail_on=(needle,),
    )
    out = _status(tmp_path, monkeypatch, db=db, authority_dir=_authority_dir(tmp_path))
    chain = out["decision_chain"]
    assert error_key in chain["query_errors"], chain["query_errors"]
    assert chain["query_errors"][error_key].startswith("OperationalError")
    if error_key == "portfolio_runs_total":
        assert chain["portfolio_runs_total"] is None
    elif error_key == "observations_total":
        assert chain["observations_total"] is None
    else:
        # The row fetch failed: every dependent field must be UNKNOWN.
        assert chain["latest_decision"]["run_id"] is None
        assert chain["latest_decision"]["proposals"] is None
        assert chain["latest_decision"]["eligible_exports"] is None


@pytest.mark.parametrize(
    ("needle", "table"),
    (
        ("SELECT count(*) FROM portfolio_run", "portfolio_runs"),
        ("SELECT count(*) FROM portfolio_state_observation", "observations"),
        ("SELECT count(*) FROM score_snapshot", "score_snapshots"),
        ("SELECT count(*) FROM committee_run", "committee_runs"),
    ),
)
def test_each_provenance_query_failure_is_unknown(tmp_path, monkeypatch, needle, table):
    db = _research_db(
        tmp_path,
        rows=(
            (
                "INSERT INTO portfolio_run VALUES (?,?,?,?)",
                [("run-1", GENUINE_RUN, "2026-09-10T20:15:00Z", "2026-09-14T01:59:34Z")],
            ),
        ),
        fail_on=(needle,),
    )
    out = _status(tmp_path, monkeypatch, db=db, authority_dir=_authority_dir(tmp_path))
    provenance = out["decision_chain"]["provenance"]
    assert provenance[table] == {"genuine": None, "acceptance": None}, provenance[table]
    assert provenance["query_errors"][table].startswith("OperationalError")
    # The un-touched sibling queries still report.
    assert all(
        provenance[key] is not None
        for key in ("portfolio_runs", "observations", "score_snapshots", "committee_runs")
        if key != table
    )


def test_proposal_query_failure_is_recorded_as_unknown_exports(tmp_path, monkeypatch):
    db = _research_db(
        tmp_path,
        rows=(
            (
                "INSERT INTO portfolio_run VALUES (?,?,?,?)",
                [("run-1", GENUINE_RUN, "2026-09-10T20:15:00Z", "2026-09-14T01:59:34Z")],
            ),
            ("INSERT INTO portfolio_state_observation VALUES (?,?)", [("d1", "run-1")]),
        ),
        fail_on=("FROM trade_proposal",),
    )
    out = _status(tmp_path, monkeypatch, db=db, authority_dir=_authority_dir(tmp_path))
    chain = out["decision_chain"]
    assert chain["query_errors"]["eligible_exports"].startswith("OperationalError")
    assert chain["latest_decision"]["proposals"] is None
    assert chain["latest_decision"]["eligible_exports"] is None


def test_experiment_db_connect_failure_is_recorded(tmp_path, monkeypatch):
    out = _status(tmp_path, monkeypatch, experiment_db=_BoomExperimentDB())
    chain = out["decision_chain"]
    assert chain["query_errors"]["validation_connect"].startswith("OperationalError")
    assert out["validation_forward"]["regime"] is None
    assert out["validation_forward"]["snapshot"] is None
    assert out["report_status"] is not None


# --------------------------------------------------------------------------
# 4. health payload failures / partial payloads -> None, never a crash
# --------------------------------------------------------------------------
def test_health_call_failures_are_recorded_and_the_payload_survives(tmp_path, monkeypatch):
    out = _status(
        tmp_path,
        monkeypatch,
        forward=RuntimeError("simulated: forward health exploded"),
        refresh=ValueError("simulated: refresh health exploded"),
    )
    errors = out["decision_chain"]["query_errors"]
    assert errors["forward_health"].startswith("RuntimeError")
    assert errors["refresh_health"].startswith("ValueError")
    assert out["research_status"] == {
        "universe_eligible": None,
        "with_price_history": None,
        "stale_names": None,
    }
    assert out["validation_forward"]["production_predictions"] is None
    assert out["validation_forward"]["matured"] is None


def test_partial_health_payloads_do_not_crash(tmp_path, monkeypatch):
    """A health payload missing keys is None-bearing, not a KeyError."""
    out = _status(
        tmp_path,
        monkeypatch,
        forward={"production_predictions": 7},
        refresh={},
    )
    assert out["research_status"]["universe_eligible"] is None
    assert out["validation_forward"]["production_predictions"] == 7
    assert out["validation_forward"]["predictions_due"] is None
    assert out["validation_forward"]["matured"] is None


def test_matured_non_mapping_is_unknown(tmp_path, monkeypatch):
    out = _status(tmp_path, monkeypatch, forward={"production_predictions": 1, "matured": ["x"]})
    assert out["validation_forward"]["matured"] is None


def test_matured_empty_mapping_is_a_known_zero(tmp_path, monkeypatch):
    out = _status(tmp_path, monkeypatch, forward={"production_predictions": 0, "matured": {}})
    assert out["validation_forward"]["matured"] == 0


# --------------------------------------------------------------------------
# 5. small helpers must never raise
# --------------------------------------------------------------------------
def test_all_total_never_raises_on_unusable_values():
    assert ops._all_total(None) is None
    assert ops._all_total({"genuine": None, "acceptance": 1}) is None
    assert ops._all_total({"genuine": 2, "acceptance": 3}) == 5
    assert ops._all_total({"genuine": "two", "acceptance": 3}) is None


def test_path_state_distinguishes_absent_from_stat_error(tmp_path):
    assert ops._path_state(tmp_path / "nope.json") == "absent"
    present = tmp_path / "present.json"
    present.write_text("{}")
    assert ops._path_state(present) == "available"
    # A directory is not "absent" -- the shape is reported as itself.
    assert ops._path_state(tmp_path) == "not_a_regular_file"

    class _Unstattable:
        def stat(self):
            raise PermissionError("EACCES")

    assert ops._path_state(_Unstattable()) == "stat_error:PermissionError"


def test_report_status_uses_the_non_conflating_path_state(tmp_path, monkeypatch):
    out = _status(tmp_path, monkeypatch)
    state = out["report_status"]["broker_analytics"]
    assert state in {"available", "absent", "not_a_regular_file"} or state.startswith(
        "stat_error:"
    ), state


def test_unreadable_runner_ledger_is_surfaced_without_crashing(tmp_path, monkeypatch):
    """The runner ledger is part of the audited surface: unreadable != 0 receipts."""

    class _Unreadable:
        def exists(self):
            return True

        def read_text(self, *args, **kwargs):
            raise PermissionError("EACCES")

    out = _status(
        tmp_path,
        monkeypatch,
        authority_dir=_authority_dir(tmp_path),
        receipts=_Unreadable(),
    )
    receipts = out["decision_chain"]["runner_receipts"]
    assert receipts["status"].startswith("unreadable:")
    assert receipts["count"] is None
    # The rest of the payload is intact.
    assert out["report_status"] is not None


# --------------------------------------------------------------------------
# 6. durable state vs the (possibly stale) research-cycle log
# --------------------------------------------------------------------------
def test_stale_cycle_status_is_labelled_historical_and_never_durable(tmp_path, monkeypatch):
    """The exact finding B regression.

    The async finalizer writes a durable portfolio run while cycle-log.jsonl
    still carries the PREVIOUS cycle's BLOCKED_NO_VALID_SCORE. The operator
    output must show the durable run AND label the cycle value as historical --
    never present the stale cycle status as the current durable decision.
    """
    db = _research_db(
        tmp_path,
        rows=(
            (
                "INSERT INTO portfolio_run VALUES (?,?,?,?)",
                [("run-1", GENUINE_RUN, "2026-09-10T20:15:00Z", "2026-09-14T01:59:34Z")],
            ),
            (
                "INSERT INTO portfolio_state_observation VALUES (?,?)",
                [("d1", "run-1"), ("d2", "run-1")],
            ),
            ("INSERT INTO trade_proposal VALUES (?,?)", [("prop-a", "d1")]),
        ),
    )
    directory = _authority_dir(tmp_path, names=("prop-a",))
    stale_cycle = json.dumps(
        {
            "as_of": "2026-09-10T20:15:00Z",
            "status": "OK",
            "decision": {"status": "BLOCKED_NO_VALID_SCORE"},
        }
    )
    out = _status(
        tmp_path, monkeypatch, db=db, cycle_log=stale_cycle + "\n", authority_dir=directory
    )

    # 1. The DURABLE run is reflected.
    assert out["portfolio_status"]["last_run"]["run_id"] == "run-1"
    assert out["portfolio_status"]["last_run"]["pipeline_run_id"] == GENUINE_RUN
    assert out["proposal_status"]["latest_decision_run_id"] == "run-1"
    assert out["proposal_status"]["proposals"] == 1
    assert out["proposal_status"]["eligible_exports"] == 1

    # 2. The stale cycle value is present but EXPLICITLY historical.
    assert out["portfolio_status"]["last_cycle_decision_status"] == "BLOCKED_NO_VALID_SCORE"
    assert out["proposal_status"]["last_cycle_decision_status"] == "BLOCKED_NO_VALID_SCORE"
    assert out["portfolio_status"]["last_cycle_as_of"] == "2026-09-10T20:15:00Z"

    # 3. No contradictory "current" status field survives anywhere.
    names = _all_field_names(out)
    assert "decision_status" not in names, names
    assert "classification" not in names, names


def test_no_current_decision_status_field_exists_even_without_a_cycle_log(tmp_path, monkeypatch):
    out = _status(tmp_path, monkeypatch, authority_dir=_authority_dir(tmp_path))
    names = _all_field_names(out)
    assert "decision_status" not in names
    assert "classification" not in names
    assert out["portfolio_status"]["last_cycle_decision_status"] is None
