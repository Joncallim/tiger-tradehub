"""Phase-1 screening orchestration (design section 6).

``run_screening`` resolves the PIT universe and a read-only as-of evidence
view, freezes canonical hashes/manifests, batch-loads facts/bars/Form 4 /
identity rows into one ``ScreenContext`` (bounded queries per source kind, no
per-security SQL loop), runs the six registered implementations in registry
order, persists each screen's full population in one transaction, verifies
expected counts, marks COMPLETE, and then runs the funnel in a separate
transaction.  Retry is insert-or-verify throughout; a differing stored hash is
a determinism error and fails the run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import tradehub_research.hunters  # noqa: F401  (populates registry)
from tradehub_research.db import ResearchDB, normalize_ts
from tradehub_research.funnel import (
    FunnelConfig,
    FunnelResultRow,
    run_funnel,
)
from tradehub_research.screen_store import ScreenStore
from tradehub_research.screens import (
    ScreenContext,
    ScreenResult,
    canonical_json,
    registered_screens,
)
from tradehub_research.snapshot import SnapshotHandle, open_snapshot_read_only


def _hash(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ScreeningConfig:
    """Operator-supplied screening configuration.

    ``holdings`` is the Phase-1 fixture/input interface for the mandatory set —
    a typed set of security ids.  There is deliberately no Tiger adapter.
    """

    funnel: FunnelConfig = field(default_factory=FunnelConfig)
    holdings: frozenset[str] = frozenset()
    universe_coverage: tuple[str, ...] = ("SUPPORTED",)
    snapshot_path: str | None = None  # optional published snapshot backing the view

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScreeningConfig:
        funnel_raw = raw.get("funnel", {})
        return cls(
            funnel=FunnelConfig(
                budget=int(funnel_raw.get("budget", 50)),
                control_count=int(funnel_raw.get("control_count", 5)),
                algorithm=str(funnel_raw.get("algorithm", "funnel-v1")),
            ),
            holdings=frozenset(raw.get("holdings", [])),
            universe_coverage=tuple(raw.get("universe_coverage", ("SUPPORTED",))),
            snapshot_path=raw.get("snapshot_path"),
        )


def load_config(path) -> ScreeningConfig:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("screening config must be a JSON object")
    return ScreeningConfig.from_dict(raw)


# ---------------------------------------------------------------------------
# Bounded batch loads (one query per source kind)
# ---------------------------------------------------------------------------


def _load_universe(db, as_of: str, coverage: tuple[str, ...]) -> list[dict[str, Any]]:
    """PIT universe: eligible membership rows knowable at as_of, for securities
    whose coverage status is selected.  One bounded query."""
    placeholders = ",".join("?" for _ in coverage)
    rows = db.execute(
        f"""
        WITH RECURSIVE visible_chain(root_id, descendant_id) AS (
            SELECT candidate.id, candidate.id FROM universe_membership candidate
            WHERE candidate.knowledge_time <= ?
              AND NOT EXISTS (
                SELECT 1 FROM universe_membership predecessor
                WHERE predecessor.id=candidate.supersedes_id
                  AND predecessor.knowledge_time <= ?)
            UNION ALL
            SELECT chain.root_id, correction.id
            FROM visible_chain chain JOIN universe_membership correction
              ON correction.supersedes_id=chain.descendant_id
            WHERE correction.knowledge_time <= ?
        ), terminal(root_id, descendant_id) AS (
            SELECT chain.root_id, chain.descendant_id FROM visible_chain chain
            WHERE NOT EXISTS (
                SELECT 1 FROM visible_chain child
                JOIN universe_membership item ON item.id=child.descendant_id
                WHERE child.root_id=chain.root_id
                  AND item.supersedes_id=chain.descendant_id)
        )
        SELECT DISTINCT m.security_id, s.sector, s.sector_coverage_status
        FROM universe_membership m
        JOIN terminal ON terminal.descendant_id=m.id
        JOIN security s ON s.security_id=m.security_id
        WHERE m.valid_from <= ? AND (m.valid_to IS NULL OR m.valid_to > ?)
          AND m.knowledge_time <= ? AND m.eligible = 1
          AND m.pat_provenance IN ('source_reported','derived_from_index')
          AND s.sector_coverage_status IN ({placeholders})
        ORDER BY m.security_id
        """,
        (as_of, as_of, as_of, as_of, as_of, as_of, *coverage),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_facts(db, as_of: str, universe: list[str]) -> dict[str, list[dict[str, Any]]]:
    """PIT-visible XBRL fact rows for the universe (one bounded query)."""
    if not universe:
        return {}
    placeholders = ",".join("?" for _ in universe)
    rows = db.execute(
        f"""
        WITH RECURSIVE visible_chain(root_id, descendant_id) AS (
            SELECT candidate.evidence_id, candidate.evidence_id
            FROM evidence_event candidate
            WHERE candidate.public_available_time IS NOT NULL
              AND candidate.public_available_time <= ?
              AND NOT EXISTS (
                SELECT 1 FROM evidence_event predecessor
                WHERE predecessor.evidence_id=candidate.supersedes_evidence_id
                  AND predecessor.public_available_time IS NOT NULL
                  AND predecessor.public_available_time <= ?)
            UNION ALL
            SELECT chain.root_id, successor.evidence_id
            FROM visible_chain chain JOIN evidence_event successor
              ON successor.supersedes_evidence_id=chain.descendant_id
            WHERE successor.public_available_time IS NOT NULL
              AND successor.public_available_time <= ?
        ), terminal(root_id, descendant_id) AS (
            SELECT chain.root_id, chain.descendant_id FROM visible_chain chain
            WHERE NOT EXISTS (
                SELECT 1 FROM visible_chain child
                WHERE child.root_id=chain.root_id
                  AND child.descendant_id IN (
                    SELECT evidence_id FROM evidence_event
                    WHERE supersedes_evidence_id=chain.descendant_id))
        )
        SELECT DISTINCT e.evidence_id, e.security_id, e.structured_fields,
               e.public_available_time
        FROM evidence_event e
        JOIN terminal t ON t.descendant_id=e.evidence_id
        WHERE e.public_available_time IS NOT NULL
          AND e.pat_provenance IN ('source_reported','derived_from_index')
          AND e.public_available_time <= ?
          AND e.withdrawn = 0
          AND json_extract(e.structured_fields,'$.record_type') = 'xbrl_fact'
          AND e.security_id IN ({placeholders})
        ORDER BY e.security_id, e.public_available_time, e.evidence_id
        """,
        (as_of, as_of, as_of, as_of, *universe),
    ).fetchall()
    return _group_fields(rows)


def _load_record_kind(
    db, as_of: str, universe: list[str], record_type: str
) -> dict[str, list[dict[str, Any]]]:
    if not universe:
        return {}
    placeholders = ",".join("?" for _ in universe)
    rows = db.execute(
        f"""
        WITH RECURSIVE visible_chain(root_id, descendant_id) AS (
            SELECT candidate.evidence_id, candidate.evidence_id
            FROM evidence_event candidate
            WHERE candidate.public_available_time IS NOT NULL
              AND candidate.public_available_time <= ?
              AND NOT EXISTS (
                SELECT 1 FROM evidence_event predecessor
                WHERE predecessor.evidence_id=candidate.supersedes_evidence_id
                  AND predecessor.public_available_time IS NOT NULL
                  AND predecessor.public_available_time <= ?)
            UNION ALL
            SELECT chain.root_id, successor.evidence_id
            FROM visible_chain chain JOIN evidence_event successor
              ON successor.supersedes_evidence_id=chain.descendant_id
            WHERE successor.public_available_time IS NOT NULL
              AND successor.public_available_time <= ?
        ), terminal(root_id, descendant_id) AS (
            SELECT chain.root_id, chain.descendant_id FROM visible_chain chain
            WHERE NOT EXISTS (
                SELECT 1 FROM visible_chain child
                WHERE child.root_id=chain.root_id
                  AND child.descendant_id IN (
                    SELECT evidence_id FROM evidence_event
                    WHERE supersedes_evidence_id=chain.descendant_id))
        )
        SELECT DISTINCT e.evidence_id, e.security_id, e.structured_fields,
               e.public_available_time
        FROM evidence_event e
        JOIN terminal t ON t.descendant_id=e.evidence_id
        WHERE e.public_available_time IS NOT NULL
          AND e.pat_provenance IN ('source_reported','derived_from_index')
          AND e.public_available_time <= ?
          AND e.withdrawn = 0
          AND json_extract(e.structured_fields,'$.record_type') = ?
          AND e.security_id IN ({placeholders})
        ORDER BY e.security_id, e.public_available_time, e.evidence_id
        """,
        (as_of, as_of, as_of, as_of, record_type, *universe),
    ).fetchall()
    return _group_fields(rows)


def _group_fields(rows) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        fields = json.loads(row["structured_fields"])
        fields["evidence_id"] = row["evidence_id"]
        fields["public_available_time"] = row["public_available_time"]
        grouped.setdefault(row["security_id"], []).append(fields)
    return grouped


def _load_identity_events(db, as_of: str, universe: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not universe:
        return {}
    placeholders = ",".join("?" for _ in universe)
    rows = db.execute(
        f"""
        SELECT id, security_id, event_type, old_value, new_value, event_time,
               public_available_time, pat_provenance, supersedes_id
        FROM security_identity_event
        WHERE public_available_time IS NOT NULL
          AND public_available_time <= ?
          AND pat_provenance IN ('source_reported','derived_from_index')
          AND security_id IN ({placeholders})
        ORDER BY security_id, public_available_time, id
        """,
        (as_of, *universe),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["security_id"], []).append(dict(row))
    return grouped


def _load_form4_coverage(db, universe: list[str]) -> dict[str, frozenset[str]]:
    """Settled EDGAR daily-index dates scanned per security (bounded query)."""
    if not universe:
        return {}
    placeholders = ",".join("?" for _ in universe)
    rows = db.execute(
        f"""
        SELECT security_id, json_extract(structured_fields,'$.index_date') AS index_date
        FROM evidence_event
        WHERE json_extract(structured_fields,'$.record_type') = 'form4_index_coverage'
          AND withdrawn = 0
          AND security_id IN ({placeholders})
        ORDER BY security_id, index_date
        """,
        tuple(universe),
    ).fetchall()
    coverage: dict[str, set[str]] = {}
    for row in rows:
        if row["index_date"]:
            coverage.setdefault(row["security_id"], set()).add(row["index_date"])
    return {sid: frozenset(dates) for sid, dates in coverage.items()}


def _load_identity_feed_state(db) -> bool:
    """The identity feed is complete when a settled feed marker exists."""
    row = db.execute(
        "SELECT COUNT(*) FROM evidence_event "
        "WHERE json_extract(structured_fields,'$.record_type')='identity_feed_marker' "
        "AND withdrawn = 0"
    ).fetchone()
    return bool(row and row[0] > 0)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _input_view_hash(
    universe: list[dict[str, Any]],
    facts: dict[str, list[dict[str, Any]]],
    price_bars: dict[str, list[dict[str, Any]]],
    form4: dict[str, list[dict[str, Any]]],
    identity_events: dict[str, list[dict[str, Any]]],
    corporate_actions: dict[str, list[dict[str, Any]]],
    form4_coverage: dict[str, frozenset[str]],
    identity_feed_complete: bool,
) -> str:
    """Canonical hash of the read-only as-of evidence view."""

    def evidence_ids(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
        return {
            sid: sorted(str(item.get("evidence_id")) for item in items)
            for sid, items in sorted(grouped.items())
        }

    view = {
        "universe": [row["security_id"] for row in universe],
        "facts": evidence_ids(facts),
        "price_bars": evidence_ids(price_bars),
        "form4": evidence_ids(form4),
        "identity_events": {
            sid: [str(item.get("id")) for item in items]
            for sid, items in sorted(identity_events.items())
        },
        "corporate_actions": evidence_ids(corporate_actions),
        "form4_coverage": {sid: sorted(dates) for sid, dates in sorted(form4_coverage.items())},
        "identity_feed_complete": identity_feed_complete,
    }
    return _hash(canonical_json(view))


def run_screening(
    as_of: str,
    snapshot_id: str | None,
    config: ScreeningConfig,
    *,
    database: ResearchDB,
) -> str:
    """Run the full Phase-1 screening pipeline; return the deterministic run_id."""
    as_of = normalize_ts(as_of)
    store = ScreenStore(database)

    specs = [(spec, fn) for spec, fn in registered_screens()]
    if not specs:
        raise RuntimeError("no screens registered; import tradehub_research.hunters.registry")

    # Resolve the read-only evidence view.  When the config names a published
    # snapshot, read through its verified handle; otherwise read the live DB.
    handle: SnapshotHandle | None = None
    if config.snapshot_path is not None:
        handle = open_snapshot_read_only(config.snapshot_path)
        connection_cm = handle.connection()
        db = connection_cm
        if snapshot_id is not None and snapshot_id != handle.manifest["snapshot_id"]:
            raise ValueError("snapshot_id does not match the configured snapshot path")
        snapshot_id = handle.manifest["snapshot_id"]
        universe_rows = _load_universe(db, as_of, config.universe_coverage)
        universe = [row["security_id"] for row in universe_rows]
        facts = _load_facts(db, as_of, universe)
        price_bars = _load_record_kind(db, as_of, universe, "price_bar")
        form4 = _load_record_kind(db, as_of, universe, "form4_transaction")
        corporate_actions_split = _load_record_kind(db, as_of, universe, "split")
        corporate_actions_div = _load_record_kind(db, as_of, universe, "dividend")
        identity_events = _load_identity_events(db, as_of, universe)
        form4_coverage = _load_form4_coverage(db, universe)
        identity_feed_complete = _load_identity_feed_state(db)
        db.close()
    else:
        with database.connect(read_only=True) as db:
            universe_rows = _load_universe(db, as_of, config.universe_coverage)
            universe = [row["security_id"] for row in universe_rows]
            facts = _load_facts(db, as_of, universe)
            price_bars = _load_record_kind(db, as_of, universe, "price_bar")
            form4 = _load_record_kind(db, as_of, universe, "form4_transaction")
            corporate_actions_split = _load_record_kind(db, as_of, universe, "split")
            corporate_actions_div = _load_record_kind(db, as_of, universe, "dividend")
            identity_events = _load_identity_events(db, as_of, universe)
            form4_coverage = _load_form4_coverage(db, universe)
            identity_feed_complete = _load_identity_feed_state(db)

    corporate_actions: dict[str, list[dict[str, Any]]] = {}
    for grouped, kind in ((corporate_actions_split, "split"), (corporate_actions_div, "dividend")):
        for sid, items in grouped.items():
            for item in items:
                item.setdefault("action_type", kind)
            corporate_actions.setdefault(sid, []).extend(items)

    sectors = {row["security_id"]: row.get("sector") for row in universe_rows}
    universe_hash = _hash(canonical_json(universe))

    manifest = []
    for spec, _fn in specs:
        store.save_screen_definition(spec)
        manifest.append({"config_hash": spec.config_hash, "expected_count": len(universe)})

    input_view_hash = _input_view_hash(
        universe_rows,
        facts,
        price_bars,
        form4,
        identity_events,
        corporate_actions,
        form4_coverage,
        identity_feed_complete,
    )

    run_id = store.begin_run(
        as_of=as_of,
        universe_hash=universe_hash,
        screen_manifest=manifest,
        funnel_config=config.funnel.as_dict(),
        input_view_hash=input_view_hash,
        expected_security_count=len(universe),
        input_snapshot_id=snapshot_id,
    )

    context = ScreenContext(
        facts=facts,
        price_bars=price_bars,
        form4=form4,
        identity_events=identity_events,
        market_caps={sid: None for sid in universe},
        universe=universe,
        as_of=as_of,
        sectors=sectors,
        form4_coverage=form4_coverage,
        identity_feed_complete=identity_feed_complete,
        corporate_actions=corporate_actions,
    )

    for spec, fn in specs:
        if store.verify_screen_population(run_id, spec.config_hash, universe):
            continue  # retry: this screen's population is already complete
        results = []
        for security_id in universe:
            payload = fn(context, security_id)
            results.append(
                ScreenResult.create(
                    run_id=run_id,
                    security_id=security_id,
                    config_hash=spec.config_hash,
                    raw_features=payload.raw_features,
                    evidence_ids=payload.evidence_ids,
                    reason_codes=payload.reason_codes,
                    sufficient_data=payload.sufficient_data,
                    passed=payload.passed,
                    confidence=payload.confidence,
                    data_quality=payload.data_quality,
                )
            )
        store.persist_screen_population(run_id, spec.config_hash, results)

    store.complete_run(run_id)

    # Funnel: separate transaction, after COMPLETE (design section 6).
    logical_material = store.logical_material(run_id)
    raw_results = store.load_results_for_funnel(run_id)
    family_rows = [
        FunnelResultRow(
            security_id=row["security_id"],
            family=row["family"],
            screen_result_id=row["screen_result_id"],
            sufficient_data=bool(row["sufficient_data"]),
            passed=bool(row["passed"]),
            confidence=float(row["confidence"]),
            data_quality=float(row["data_quality"]),
            evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
        )
        for row in raw_results
    ]
    candidates, flags = run_funnel(
        run_id=run_id,
        logical_material=logical_material,
        config=config.funnel,
        universe=universe,
        holdings=set(config.holdings),
        results=family_rows,
        sectors=sectors,
        cluster_lookup=store.cluster_ids_by_evidence(),
    )
    store.persist_candidates(run_id, candidates)
    if flags:
        # Blocking condition for downstream committee launch.  Stored as a
        # run-note row in pipeline_run.failure_json is forbidden on COMPLETE
        # (immutable trigger), so surface it through the return value's logs.
        import logging

        logging.getLogger(__name__).warning(
            "run %s has blocking flags: %s", run_id, ",".join(flags)
        )
    return run_id


__all__ = [
    "ScreeningConfig",
    "load_config",
    "run_screening",
]
