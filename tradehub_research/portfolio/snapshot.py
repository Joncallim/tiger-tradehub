"""Typed portfolio snapshot and signal-input import (deterministic, versioned).

A portfolio snapshot is one immutable manifest plus holding and per-security
market-input rows.  Money is integer micro-USD, weights/ratios integer ppm,
quantities integer micro-shares.  ``empty-known`` and ``unknown`` are
distinct statuses.  IDs hash canonical material; imports are equality-checked
idempotent (an existing ID with different bytes is rejected).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.portfolio.types import (
    C,
    D,
    SignalSourceKind,
    ValueStatus,
)


@contextmanager
def _nullcontext(value: Any):
    yield value


ALLOWED_STATUSES = ("KNOWN", "STALE", "UNKNOWN")

SNAPSHOT_TAG = "portfolio-snapshot-v1"
SIGNAL_TAG = "portfolio-signal-v1"


def _validate_status(value: Any, path: str, *, known_only: bool = False) -> ValueStatus:
    if value not in ALLOWED_STATUSES:
        raise ValueError(f"{path} invalid status {value!r}")
    if known_only and value != "KNOWN":
        raise ValueError(f"{path} must be KNOWN for this field, got {value!r}")
    return ValueStatus(value)


def _validate_security_id(value: Any, path: str) -> str:
    """Strict one-line identifier grammar: no control characters, bounded.

    Rendered identifiers (briefings, JSON) must never carry newlines or
    control characters: a poisoned security_id must not be able to inject
    fake sections or instructions into human-facing output.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} must be a non-empty string")
    if len(value) > 64:
        raise ValueError(f"{path} exceeds 64 characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{path} contains control characters")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{path} must be a single line")
    return value


def _orthogonal(value: Any, status: str, path: str) -> None:
    """Value presence must match status: KNOWN/STALE => value, UNKNOWN => None."""
    if status == "UNKNOWN":
        if value is not None:
            raise ValueError(f"{path} must be null when status is UNKNOWN")
    elif value is None:
        raise ValueError(f"{path} must be set when status is {status}")


@dataclass(frozen=True)
class PortfolioSnapshot:
    snapshot_id: str
    input_hash: str
    as_of: str
    currency: str
    cash_microusd: int | None
    cash_status: ValueStatus
    nav_microusd: int | None
    valuation_status: ValueStatus
    holdings_status: ValueStatus
    provenance: dict[str, Any]
    holdings: tuple[dict[str, Any], ...]
    market_inputs: tuple[dict[str, Any], ...]

    def holding(self, security_id: str) -> dict[str, Any] | None:
        for row in self.holdings:
            if row["security_id"] == security_id:
                return row
        return None

    def market_input(self, security_id: str) -> dict[str, Any] | None:
        for row in self.market_inputs:
            if row["security_id"] == security_id:
                return row
        return None

    def sector_total_ppm(self, sector: str | None) -> int:
        """Sum of holding market values in a sector as ppm of NAV (0 when unknown)."""
        if self.nav_microusd is None:
            return 0
        total_microusd = 0
        for row in self.holdings:
            if row["sector"] != sector:
                continue
            if row["market_value_microusd"] is None:
                continue
            total_microusd += row["market_value_microusd"]
        return round(total_microusd * 1_000_000 / self.nav_microusd)


@dataclass(frozen=True)
class SignalInput:
    signal_input_id: str
    input_hash: str
    security_id: str
    as_of: str
    remaining_opportunity_ppm: int | None
    opportunity_status: ValueStatus
    source_kind: SignalSourceKind
    evidence_ids: tuple[str, ...]


def canonical_snapshot_material(
    as_of: str,
    currency: str,
    cash: dict[str, Any],
    valuation: dict[str, Any],
    holdings_status: str,
    provenance: dict[str, Any],
    holdings: list[dict[str, Any]],
    market_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Canonical material for snapshot identity (ids/recorded_at excluded)."""
    holdings_material = []
    for row in sorted(holdings, key=lambda item: item["security_id"]):
        holdings_material.append(
            {
                "security_id": row["security_id"],
                "quantity_microunits": row["quantity_microunits"],
                "sellable_quantity_microunits": row.get("sellable_quantity_microunits"),
                "sellable_status": row.get("sellable_status", "KNOWN"),
                "market_value_microusd": row.get("market_value_microusd"),
                "valuation_status": row.get("valuation_status", "KNOWN"),
                "sector": row.get("sector"),
                "sector_status": row.get("sector_status", "KNOWN"),
            }
        )
    market_material = []
    for row in sorted(market_inputs, key=lambda item: item["security_id"]):
        market_material.append(
            {
                "security_id": row["security_id"],
                "mark_price_microusd": row.get("mark_price_microusd"),
                "price_as_of": row.get("price_as_of"),
                "price_status": row.get("price_status", "KNOWN"),
                "avg_dollar_volume_microusd": row.get("avg_dollar_volume_microusd"),
                "liquidity_as_of": row.get("liquidity_as_of"),
                "liquidity_status": row.get("liquidity_status", "KNOWN"),
                "evidence_ids": sorted(row.get("evidence_ids", [])),
            }
        )
    return {
        "as_of": as_of,
        "currency": currency,
        "cash_microusd": cash["cash_microusd"],
        "cash_status": cash["cash_status"],
        "nav_microusd": valuation["nav_microusd"],
        "valuation_status": valuation["valuation_status"],
        "holdings_status": holdings_status,
        "provenance": provenance,
        "holdings": holdings_material,
        "market_inputs": market_material,
    }


def build_snapshot(
    as_of: str,
    *,
    currency: str = "USD",
    cash_microusd: int | None = None,
    cash_status: str = "KNOWN",
    nav_microusd: int | None = None,
    valuation_status: str = "KNOWN",
    holdings_status: str = "KNOWN",
    provenance: dict[str, Any] | None = None,
    holdings: list[dict[str, Any]] | None = None,
    market_inputs: list[dict[str, Any]] | None = None,
) -> PortfolioSnapshot:
    """Validate a snapshot in memory and derive its deterministic identity."""
    if currency != "USD":
        raise ValueError("only USD snapshots are supported")
    holdings = holdings or []
    market_inputs = market_inputs or []
    _validate_status(cash_status, "cash_status")
    _validate_status(valuation_status, "valuation_status")
    _validate_status(holdings_status, "holdings_status")
    _orthogonal(cash_microusd, cash_status, "cash_microusd")
    _orthogonal(nav_microusd, valuation_status, "nav_microusd")
    if cash_microusd is not None and (
        not isinstance(cash_microusd, int) or isinstance(cash_microusd, bool) or cash_microusd < 0
    ):
        raise ValueError("cash_microusd must be a non-negative integer")
    if nav_microusd is not None and (
        not isinstance(nav_microusd, int) or isinstance(nav_microusd, bool) or nav_microusd <= 0
    ):
        raise ValueError("nav_microusd must be a positive integer")
    if holdings_status == "UNKNOWN" and holdings:
        raise ValueError(
            "holdings_status=UNKNOWN cannot carry child holding rows: unknown "
            "holdings must never be trusted as an empty book"
        )
    if provenance is None:
        provenance = {"kind": "fixture"}
    if not isinstance(provenance, dict):
        raise ValueError("provenance must be an object")

    seen: set[str] = set()
    for row in holdings:
        security_id = _validate_security_id(row.get("security_id"), "holding security_id")
        if security_id in seen:
            raise ValueError(f"duplicate holding {security_id!r}")
        seen.add(security_id)
        quantity = row.get("quantity_microunits")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            raise ValueError(f"holding {security_id} quantity_microunits invalid")
        quantity_status = _validate_status(
            row.get("quantity_status", "KNOWN"), f"holding {security_id}.quantity_status"
        )
        if quantity_status != "KNOWN" and quantity != 0:
            raise ValueError(
                f"holding {security_id} quantity must be zero when quantity_status is not KNOWN"
            )
        sellable_status = _validate_status(
            row.get("sellable_status", "KNOWN"), f"holding {security_id}.sellable_status"
        )
        sellable = row.get("sellable_quantity_microunits")
        _orthogonal(
            sellable, sellable_status.value, f"holding {security_id}.sellable_quantity_microunits"
        )
        if sellable is not None and (not isinstance(sellable, int) or isinstance(sellable, bool)):
            raise ValueError(f"holding {security_id} sellable_quantity must be an integer")
        if sellable is not None and not 0 <= sellable <= quantity:
            raise ValueError(f"holding {security_id} sellable quantity outside [0, quantity]")
        valuation = _validate_status(
            row.get("valuation_status", "KNOWN"), f"holding {security_id}.valuation_status"
        )
        market_value = row.get("market_value_microusd")
        _orthogonal(market_value, valuation.value, f"holding {security_id}.market_value_microusd")
        if market_value is not None and (
            not isinstance(market_value, int) or isinstance(market_value, bool)
        ):
            raise ValueError(f"holding {security_id} market_value must be an integer")
        if market_value is not None and market_value < 0:
            raise ValueError(f"holding {security_id} market_value cannot be negative")
        sector_status = _validate_status(
            row.get("sector_status", "KNOWN"),
            f"holding {security_id}.sector_status",
            known_only=False,
        )
        sector = row.get("sector")
        _orthogonal(sector, sector_status.value, f"holding {security_id}.sector")
        if sector is not None and not sector.strip():
            raise ValueError(f"holding {security_id} sector must be non-empty")

    seen_market: set[str] = set()
    for row in market_inputs:
        security_id = _validate_security_id(row.get("security_id"), "market input security_id")
        if security_id in seen_market:
            raise ValueError(f"duplicate market input {security_id!r}")
        seen_market.add(security_id)
        price_status = _validate_status(
            row.get("price_status", "KNOWN"), f"market {security_id}.price_status"
        )
        mark = row.get("mark_price_microusd")
        price_as_of = row.get("price_as_of")
        _orthogonal(mark, price_status.value, f"market {security_id}.mark_price_microusd")
        _orthogonal(price_as_of, price_status.value, f"market {security_id}.price_as_of")
        if mark is not None and (not isinstance(mark, int) or isinstance(mark, bool) or mark <= 0):
            raise ValueError(f"market {security_id} mark_price must be a positive integer")
        liquidity_status = _validate_status(
            row.get("liquidity_status", "KNOWN"), f"market {security_id}.liquidity_status"
        )
        adv = row.get("avg_dollar_volume_microusd")
        liquidity_as_of = row.get("liquidity_as_of")
        _orthogonal(adv, liquidity_status.value, f"market {security_id}.avg_dollar_volume_microusd")
        _orthogonal(
            liquidity_as_of, liquidity_status.value, f"market {security_id}.liquidity_as_of"
        )
        if adv is not None and (not isinstance(adv, int) or isinstance(adv, bool) or adv < 0):
            raise ValueError(
                f"market {security_id} avg_dollar_volume must be a non-negative integer"
            )
        evidence_ids = row.get("evidence_ids", [])
        if not isinstance(evidence_ids, list) or any(
            not isinstance(item, str) for item in evidence_ids
        ):
            raise ValueError(f"market {security_id} evidence_ids must be a list of strings")

    # NAV / child-sum reconciliation: cash + holdings market values == NAV exactly.
    if nav_microusd is not None:
        holding_sum = 0
        all_holding_values_known = True
        for row in holdings:
            if row.get("valuation_status", "KNOWN") == "UNKNOWN":
                all_holding_values_known = False
                break
            holding_sum += row["market_value_microusd"]
        if not all_holding_values_known:
            raise ValueError("nav_microusd KNOWN requires every holding market value KNOWN")
        if cash_microusd is None:
            raise ValueError("nav_microusd KNOWN requires cash KNOWN")
        if cash_microusd + holding_sum != nav_microusd:
            raise ValueError(
                f"snapshot NAV mismatch: cash + holdings = {cash_microusd + holding_sum}, "
                f"nav = {nav_microusd}"
            )

    material = canonical_snapshot_material(
        as_of,
        currency,
        {"cash_microusd": cash_microusd, "cash_status": cash_status},
        {"nav_microusd": nav_microusd, "valuation_status": valuation_status},
        holdings_status,
        provenance,
        holdings,
        market_inputs,
    )
    input_hash = C(material)
    snapshot_id = D(SNAPSHOT_TAG, input_hash)
    # Storage order is canonicalized by security_id so identity and iteration
    # order can never diverge (a reordered input yields the same snapshot AND
    # the same briefing/correlation iteration order).
    normalized_holdings = tuple(
        _normalize_holding(row) for row in sorted(holdings, key=lambda item: item["security_id"])
    )
    normalized_market = tuple(
        _normalize_market_input(row)
        for row in sorted(market_inputs, key=lambda item: item["security_id"])
    )
    return PortfolioSnapshot(
        snapshot_id=snapshot_id,
        input_hash=input_hash,
        as_of=as_of,
        currency=currency,
        cash_microusd=cash_microusd,
        cash_status=ValueStatus(cash_status),
        nav_microusd=nav_microusd,
        valuation_status=ValueStatus(valuation_status),
        holdings_status=ValueStatus(holdings_status),
        provenance=provenance,
        holdings=normalized_holdings,
        market_inputs=normalized_market,
    )


def _normalize_holding(row: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a holding row with explicit status fields (KNOWN when present)."""
    normalized = dict(row)
    normalized.setdefault("quantity_status", "KNOWN")
    if (
        normalized.get("market_value_microusd") is None
        and normalized.get("valuation_status") is None
    ):
        normalized["valuation_status"] = "UNKNOWN"
    elif normalized.get("valuation_status") is None:
        normalized["valuation_status"] = "KNOWN"
    if (
        normalized.get("sellable_quantity_microunits") is None
        and normalized.get("sellable_status") is None
    ):
        normalized["sellable_status"] = "UNKNOWN"
    elif normalized.get("sellable_status") is None:
        normalized["sellable_status"] = "KNOWN"
    if normalized.get("sector") is None and normalized.get("sector_status") is None:
        normalized["sector_status"] = "UNKNOWN"
    elif normalized.get("sector_status") is None:
        normalized["sector_status"] = "KNOWN"
    return normalized


def _normalize_market_input(row: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a market-input row with explicit status fields (KNOWN when present)."""
    normalized = dict(row)
    if normalized.get("mark_price_microusd") is None and normalized.get("price_status") is None:
        normalized["price_status"] = "UNKNOWN"
    elif normalized.get("price_status") is None:
        normalized["price_status"] = "KNOWN"
    if (
        normalized.get("avg_dollar_volume_microusd") is None
        and normalized.get("liquidity_status") is None
    ):
        normalized["liquidity_status"] = "UNKNOWN"
    elif normalized.get("liquidity_status") is None:
        normalized["liquidity_status"] = "KNOWN"
    return normalized


def build_signal_input(
    security_id: str,
    as_of: str,
    *,
    remaining_opportunity_ppm: int | None = None,
    opportunity_status: str | None = None,
    source_kind: str = "FIXTURE",
    evidence_ids: list[str] | None = None,
) -> SignalInput:
    """Validate a signal input and derive its deterministic identity."""
    security_id = _validate_security_id(security_id, "signal input security_id")
    if opportunity_status is None:
        opportunity_status = "KNOWN" if remaining_opportunity_ppm is not None else "UNKNOWN"
    _validate_status(opportunity_status, "opportunity_status")
    _orthogonal(remaining_opportunity_ppm, opportunity_status, "remaining_opportunity_ppm")
    if remaining_opportunity_ppm is not None and not 0 <= remaining_opportunity_ppm <= 1_000_000:
        raise ValueError("remaining_opportunity_ppm out of range")
    try:
        kind = SignalSourceKind(source_kind)
    except ValueError as exc:
        raise ValueError(f"invalid signal source_kind {source_kind!r}") from exc
    evidence_ids = evidence_ids or []
    if any(not isinstance(item, str) for item in evidence_ids):
        raise ValueError("evidence_ids must be a list of strings")
    material = {
        "security_id": security_id,
        "as_of": as_of,
        "remaining_opportunity_ppm": remaining_opportunity_ppm,
        "opportunity_status": opportunity_status,
        "source_kind": kind.value,
        "evidence_ids": sorted(evidence_ids),
    }
    input_hash = C(material)
    return SignalInput(
        signal_input_id=D(SIGNAL_TAG, input_hash),
        input_hash=input_hash,
        security_id=security_id,
        as_of=as_of,
        remaining_opportunity_ppm=remaining_opportunity_ppm,
        opportunity_status=ValueStatus(opportunity_status),
        source_kind=kind,
        evidence_ids=tuple(sorted(evidence_ids)),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SnapshotStore:
    """Equality-checked idempotent writes for snapshots and signal inputs."""

    def __init__(self, database: ResearchDB):
        self.database = database

    def save_snapshot(
        self, snapshot: PortfolioSnapshot, *, recorded_at: str | None = None, db: Any | None = None
    ) -> str:
        recorded_at = recorded_at or utc_now()
        context = self.database.connect() if db is None else _nullcontext(db)
        with context as db:
            existing = db.execute(
                "SELECT input_hash FROM portfolio_snapshot WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != snapshot.input_hash:
                    raise ValueError("snapshot_id exists with different content (byte mismatch)")
                return snapshot.snapshot_id
            self._validate_evidence_references(db, snapshot)
            db.execute(
                "INSERT INTO portfolio_snapshot("
                "snapshot_id,as_of,currency,cash_microusd,cash_status,nav_microusd,"
                "valuation_status,holdings_status,provenance_json,input_hash,recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot.snapshot_id,
                    snapshot.as_of,
                    snapshot.currency,
                    snapshot.cash_microusd,
                    snapshot.cash_status.value,
                    snapshot.nav_microusd,
                    snapshot.valuation_status.value,
                    snapshot.holdings_status.value,
                    json_text(snapshot.provenance),
                    snapshot.input_hash,
                    recorded_at,
                ),
            )
            for row in snapshot.holdings:
                db.execute(
                    "INSERT INTO portfolio_holding("
                    "snapshot_id,security_id,quantity_microunits,sellable_quantity_microunits,"
                    "sellable_status,market_value_microusd,valuation_status,sector,sector_status,"
                    "provenance_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot.snapshot_id,
                        row["security_id"],
                        row["quantity_microunits"],
                        row.get("sellable_quantity_microunits"),
                        row.get("sellable_status", "KNOWN"),
                        row.get("market_value_microusd"),
                        row.get("valuation_status", "KNOWN"),
                        row.get("sector"),
                        row.get("sector_status", "KNOWN"),
                        json_text(row.get("provenance", {})),
                    ),
                )
            for row in snapshot.market_inputs:
                db.execute(
                    "INSERT INTO portfolio_market_input("
                    "snapshot_id,security_id,mark_price_microusd,price_as_of,price_status,"
                    "avg_dollar_volume_microusd,liquidity_as_of,liquidity_status,evidence_ids_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot.snapshot_id,
                        row["security_id"],
                        row.get("mark_price_microusd"),
                        row.get("price_as_of"),
                        row.get("price_status", "KNOWN"),
                        row.get("avg_dollar_volume_microusd"),
                        row.get("liquidity_as_of"),
                        row.get("liquidity_status", "KNOWN"),
                        json_text(sorted(row.get("evidence_ids", []))),
                    ),
                )
        return snapshot.snapshot_id

    def _validate_evidence_references(self, db: Any, snapshot: PortfolioSnapshot) -> None:
        """Every referenced evidence ID must exist, match the security, and be
        a price/action record — an unauthenticated citation must not be able to
        underpin KNOWN market inputs."""
        import json as _json

        references: list[tuple[str, str]] = []  # (security_id, evidence_id)
        for row in snapshot.market_inputs:
            for evidence_id in row.get("evidence_ids", []):
                references.append((row["security_id"], evidence_id))
        if not references:
            return
        for security_id, evidence_id in references:
            record = db.execute(
                "SELECT security_id,structured_fields,withdrawn,pat_provenance,"
                "public_available_time FROM evidence_event WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            if record is None:
                raise ValueError(
                    f"market input for {security_id} references unknown evidence {evidence_id!r}"
                )
            if record["security_id"] != security_id:
                raise ValueError(
                    f"market input for {security_id} references evidence of "
                    f"another security {record['security_id']!r}"
                )
            fields = _json.loads(record["structured_fields"])
            if fields.get("record_type") not in ("price_bar", "split", "dividend"):
                raise ValueError(
                    f"market input for {security_id} references non-price evidence "
                    f"{evidence_id!r} (record_type={fields.get('record_type')!r})"
                )
            if record["withdrawn"]:
                raise ValueError(
                    f"market input for {security_id} references withdrawn evidence {evidence_id!r}"
                )
            if record["pat_provenance"] not in ("source_reported", "derived_from_index"):
                raise ValueError(
                    f"market input for {security_id} references evidence {evidence_id!r} "
                    f"with non-approved provenance {record['pat_provenance']!r}"
                )
            if (
                record["public_available_time"] is not None
                and record["public_available_time"] > snapshot.as_of
            ):
                raise ValueError(
                    f"market input for {security_id} references future evidence {evidence_id!r} "
                    f"(PAT {record['public_available_time']} > snapshot {snapshot.as_of})"
                )

    def save_signal_input(
        self, signal: SignalInput, *, recorded_at: str | None = None, db: Any | None = None
    ) -> str:
        recorded_at = recorded_at or utc_now()
        context = self.database.connect() if db is None else _nullcontext(db)
        with context as db:
            existing = db.execute(
                "SELECT input_hash FROM portfolio_signal_input WHERE signal_input_id=?",
                (signal.signal_input_id,),
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != signal.input_hash:
                    raise ValueError(
                        "signal_input_id exists with different content (byte mismatch)"
                    )
                return signal.signal_input_id
            db.execute(
                "INSERT INTO portfolio_signal_input("
                "signal_input_id,security_id,as_of,remaining_opportunity_ppm,opportunity_status,"
                "source_kind,evidence_ids_json,input_hash,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    signal.signal_input_id,
                    signal.security_id,
                    signal.as_of,
                    signal.remaining_opportunity_ppm,
                    signal.opportunity_status.value,
                    signal.source_kind.value,
                    json_text(list(signal.evidence_ids)),
                    signal.input_hash,
                    recorded_at,
                ),
            )
        return signal.signal_input_id


def json_text(value: Any) -> str:
    from tradehub_research.portfolio.types import json_roundtrip

    return json_roundtrip(value)
