"""Research-side reader for the credential-free PAPER portfolio handoff.

This is a narrow data-import boundary.  It never imports execution modules,
opens broker clients, or creates proposals.  Bad state is represented by an
exception so callers can record ``BLOCKED_MISSING_PORTFOLIO_STATE``.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tradehub_research.db import ResearchDB, normalize_ts
from tradehub_research.portfolio.snapshot import PortfolioSnapshot, build_snapshot

SCHEMA_VERSION = "paper-portfolio-handoff-v2"
_USABLE_ACCOUNT_STATUSES = frozenset({"Open", "Funded", "New"})
_MAX_AGE_SECONDS = 78 * 3600


class PortfolioHandoffUnavailable(ValueError):
    """The execution-owned account observation is missing or unsafe to use."""


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PortfolioHandoffUnavailable(f"{field} is missing or invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PortfolioHandoffUnavailable(f"{field} is not decimal") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise PortfolioHandoffUnavailable(f"{field} is outside allowed range")
    return result


def _micro(value: Any, field: str, *, positive: bool = False) -> int:
    return int(
        (_decimal(value, field, positive=positive) * Decimal(1_000_000)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise PortfolioHandoffUnavailable(f"{field} is missing")
    try:
        return datetime.fromisoformat(normalize_ts(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortfolioHandoffUnavailable(f"{field} is invalid") from exc


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PortfolioHandoffUnavailable("portfolio handoff unavailable") from exc
    if not isinstance(loaded, dict):
        raise PortfolioHandoffUnavailable("portfolio handoff is not an object")
    return loaded


def _security_by_ticker(database: ResearchDB, ticker: str) -> tuple[str, str | None]:
    with database.connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT security_id,sector FROM security "
            "WHERE canonical_ticker=? AND delisted_at IS NULL",
            (ticker,),
        ).fetchall()
    if len(rows) != 1:
        raise PortfolioHandoffUnavailable(f"ticker {ticker!r} does not map uniquely to security")
    return str(rows[0]["security_id"]), rows[0]["sector"]


def load_paper_portfolio_snapshot(
    database: ResearchDB,
    *,
    decision_as_of: str,
    pipeline_run_id: str,
    path: Path,
) -> PortfolioSnapshot:
    """Validate a v2 execution handoff and construct the typed snapshot.

    We demand known cash, NAV, positions, sellability, and marks.  An empty
    list is a legitimate known-empty portfolio.  A nonempty list is accepted
    only when every ticker maps uniquely to research identity and its broker
    valuation exactly reconciles with cash and NAV.
    """
    payload = _read_payload(path)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise PortfolioHandoffUnavailable("unexpected portfolio handoff schema")
    if payload.get("account_type") != "PAPER" or payload.get("environment") not in {
        "PAPER_SANDBOX",
        "LIVE",
    }:
        raise PortfolioHandoffUnavailable("handoff does not prove PAPER account")
    if payload.get("account_status") not in _USABLE_ACCOUNT_STATUSES:
        raise PortfolioHandoffUnavailable("handoff account status is unusable")
    observed_at = _parse_time(payload.get("as_of"), "handoff as_of")
    decision_time = _parse_time(decision_as_of, "decision as_of")
    age = (decision_time - observed_at).total_seconds()
    if age < 0 or age > _MAX_AGE_SECONDS:
        raise PortfolioHandoffUnavailable("portfolio handoff is stale or from the future")
    if not isinstance(payload.get("positions"), list):
        raise PortfolioHandoffUnavailable("handoff positions are missing")
    cash = _micro(payload.get("cash"), "cash")
    nav = _micro(payload.get("nav"), "nav", positive=True)
    holdings: list[dict[str, Any]] = []
    market_inputs: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()
    for index, position in enumerate(payload["positions"]):
        if not isinstance(position, dict):
            raise PortfolioHandoffUnavailable(f"position {index} is not an object")
        ticker = position.get("ticker")
        if (
            not isinstance(ticker, str)
            or not ticker
            or ticker != ticker.upper()
            or ticker in seen_tickers
        ):
            raise PortfolioHandoffUnavailable(f"position {index} ticker is invalid or duplicated")
        seen_tickers.add(ticker)
        if position.get("currency") != "USD":
            raise PortfolioHandoffUnavailable(f"position {ticker} is not USD")
        security_id, sector = _security_by_ticker(database, ticker)
        quantity = _micro(position.get("quantity"), f"position {ticker} quantity")
        sellable = _micro(position.get("sellable_quantity"), f"position {ticker} sellable")
        if sellable > quantity:
            raise PortfolioHandoffUnavailable(f"position {ticker} sellable exceeds quantity")
        market_value = _micro(position.get("market_value"), f"position {ticker} market_value")
        mark = _micro(position.get("mark_price"), f"position {ticker} mark_price", positive=True)
        implied_value = quantity * mark // 1_000_000
        if market_value != implied_value:
            raise PortfolioHandoffUnavailable(
                f"position {ticker} market value does not match quantity x mark"
            )
        holdings.append(
            {
                "security_id": security_id,
                "quantity_microunits": quantity,
                "sellable_quantity_microunits": sellable,
                "market_value_microusd": market_value,
                "sector": sector,
                "provenance": {"kind": "execution_sanitized_paper_handoff_v2", "ticker": ticker},
            }
        )
        market_inputs.append(
            {
                "security_id": security_id,
                "mark_price_microusd": mark,
                "price_as_of": normalize_ts(str(payload["as_of"])),
                "avg_dollar_volume_microusd": 0,
                "liquidity_as_of": normalize_ts(str(payload["as_of"])),
                "evidence_ids": [],
            }
        )
    if cash + sum(item["market_value_microusd"] for item in holdings) != nav:
        raise PortfolioHandoffUnavailable("handoff NAV does not reconcile with cash and holdings")
    return build_snapshot(
        normalize_ts(str(payload["as_of"])),
        cash_microusd=cash,
        nav_microusd=nav,
        holdings_status="KNOWN",
        provenance={
            "kind": "execution_sanitized_paper_handoff_v2",
            "pipeline_run_id": pipeline_run_id,
            "handoff_as_of": normalize_ts(str(payload["as_of"])),
        },
        holdings=holdings,
        market_inputs=market_inputs,
    )
