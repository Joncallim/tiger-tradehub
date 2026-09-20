"""Downstream protection for securities with stale required market data.

Contract
--------
A security whose required market data is behind the expected session must not
participate in breakout scans, rankings, momentum/valuation screens, automated
strategy signals, portfolio recommendations or execution decisions. It is marked
``DATA_STALE`` until freshness is restored, and then automatically returned to
service.

Rules
-----
* **Never fabricate.** No forward-fill, no price substitution, no carrying the
  last close forward. A quarantined security yields NO bars, so every consumer
  sees "insufficient data" and drops out honestly.
* **Scoped, not global.** Only the `price_bar` series is withheld. Datasets that
  are not stale (SEC fundamentals, corporate actions) keep flowing, so a strategy
  that does not depend on a fresh price can still run -- the decision is per
  dataset, not a blanket disable.
* **Append-only and idempotent.** Re-marking is a no-op; clearing is recorded
  rather than deleted, so the audit trail survives.
* **Fail-open on a corrupt file.** A guard that cannot be read must not silently
  quarantine the whole universe -- it degrades to "nothing quarantined" and the
  freshness report carries the alert instead.

Storage follows the established `retired_securities.json` idiom: a JSON file
under the research dir, not a schema migration on the live ledger.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_QUARANTINE_FILENAME = "data_stale.json"
_cache: dict | None = None
_cache_key: tuple[str, float] | None = None


def quarantine_path(research_dir: Path | None = None) -> Path:
    """Location of the quarantine file.

    It lives at the research-dir root, NOT under ``autonomy/``. That directory
    is owned by ``tradehub-autonomy`` with an ACL granting the research identity
    ``r-x`` only, so the freshness/remediation path (which runs as
    ``tradehub-research``) cannot create files there. Autonomy-owned state stays
    untouched; the research identity owns a file it must write. The research
    cycle reads it under the same identity.
    """
    base = research_dir or Path(os.environ.get("TRADEHUB_RESEARCH_DIR", "data/research"))
    return Path(base) / _QUARANTINE_FILENAME


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return []  # fail-open: a corrupt guard must not quarantine everything
    return [item for item in data if isinstance(item, dict)]


def load_quarantine(research_dir: Path | None = None) -> dict[str, dict]:
    """security_id -> active quarantine record (uncleared entries only)."""
    out: dict[str, dict] = {}
    for item in _load(quarantine_path(research_dir)):
        if item.get("cleared_at"):
            continue
        sid = str(item.get("security_id") or "")
        if sid:
            out[sid] = item
    return out


def stale_security_ids(research_dir: Path | None = None) -> frozenset[str]:
    """Hot-path accessor: cached by file mtime so hunters pay one stat() each."""
    global _cache, _cache_key
    path = quarantine_path(research_dir)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime)
    except OSError:
        _cache, _cache_key = {}, None
        return frozenset()
    if _cache is None or _cache_key != key:
        _cache = load_quarantine(research_dir)
        _cache_key = key
    return frozenset(_cache)


def is_data_stale(security_id: str, research_dir: Path | None = None) -> bool:
    return str(security_id) in stale_security_ids(research_dir)


def stale_reason(security_id: str, research_dir: Path | None = None) -> str | None:
    record = load_quarantine(research_dir).get(str(security_id))
    return (record or {}).get("reason")


def _write(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def mark_data_stale(
    security_id: str,
    *,
    ticker: str | None = None,
    expected_session: str | None = None,
    reason: str | None = None,
    research_dir: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Quarantine a security.

    Returns True only when a NEW quarantine was created, so callers can report
    newly-marked symbols idempotently. An existing active record is never
    duplicated -- but its metadata IS refreshed when the classification or
    expected session has changed, because a stale reason string makes the audit
    trail contradict the audit (METRY kept reading FETCHED_NOT_STORED after it
    had been correctly reclassified as an unpublished-session timing fault).
    ``classified_at`` preserves when the security was FIRST quarantined;
    ``reason_updated_at`` records the refresh.
    """
    path = quarantine_path(research_dir)
    items = _load(path)
    sid = str(security_id)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    for item in items:
        if str(item.get("security_id")) == sid and not item.get("cleared_at"):
            changed = False
            for field, value in (
                ("ticker", ticker),
                ("expected_session", expected_session),
                ("reason", reason),
            ):
                if value is not None and item.get(field) != value:
                    item[field] = value
                    changed = True
            if changed:
                item["reason_updated_at"] = stamp
                _write(path, items)
                _invalidate()
            return False
    items.append(
        {
            "security_id": sid,
            "ticker": ticker,
            "expected_session": expected_session,
            "reason": reason,
            "classified_at": stamp,
            "reason_updated_at": None,
            "cleared_at": None,
        }
    )
    _write(path, items)
    _invalidate()
    return True


def clear_data_stale(
    security_id: str, *, research_dir: Path | None = None, now: datetime | None = None
) -> bool:
    """Return a security to service. Idempotent. Returns True when cleared."""
    path = quarantine_path(research_dir)
    items = _load(path)
    sid = str(security_id)
    changed = False
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    for item in items:
        if str(item.get("security_id")) == sid and not item.get("cleared_at"):
            item["cleared_at"] = stamp
            changed = True
    if changed:
        _write(path, items)
        _invalidate()
    return changed


def _invalidate() -> None:
    global _cache, _cache_key
    _cache, _cache_key = None, None


def sync_quarantine(
    stale: list[dict],
    *,
    expected_session: str,
    research_dir: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Reconcile the quarantine set with the current audit outcome.

    ``stale`` is the list of still-stale securities (dicts with security_id,
    ticker, classification). Anything formerly quarantined that is no longer
    stale is cleared -- a successfully repaired security is automatically
    restored to downstream signals.
    """
    want = {str(row["security_id"]): row for row in stale if row.get("security_id")}
    marked, cleared = [], []
    for sid, row in want.items():
        if mark_data_stale(
            sid,
            ticker=row.get("ticker"),
            expected_session=expected_session,
            reason=row.get("classification"),
            research_dir=research_dir,
            now=now,
        ):
            marked.append(row.get("ticker"))
    for sid, record in load_quarantine(research_dir).items():
        if sid not in want:
            if clear_data_stale(sid, research_dir=research_dir, now=now):
                cleared.append(record.get("ticker"))
    return {
        "quarantined": sorted(t for t in marked if t),
        "cleared": sorted(t for t in cleared if t),
        "active": len(load_quarantine(research_dir)),
    }


__all__ = [
    "clear_data_stale",
    "is_data_stale",
    "load_quarantine",
    "mark_data_stale",
    "quarantine_path",
    "stale_reason",
    "stale_security_ids",
    "sync_quarantine",
]
