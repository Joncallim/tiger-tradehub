"""Portfolio risk checks: concentration, sector, correlation, volatility, liquidity.

BUY/ADD requires trusted holdings, NAV, cash, sector, mark, price history,
volatility, correlation, and liquidity; any critical UNKNOWN or STALE input
blocks (``unknown_increase`` is always BLOCK).  SELL requires trusted quantity,
sellable quantity, valuation/mark, NAV, and liquidity; unknown concentration/
correlation/volatility follows ``unknown_decrease`` (LIMITED or BLOCK), never
relabelled PASS.  Factor and drawdown seams report NOT_AVAILABLE; a required
seam blocks the decision — no fake exposure is ever fabricated.

Correlation is fail-closed for increases: a material holding whose pairwise
correlation cannot be computed (insufficient overlap, zero variance, missing
history) blocks the BUY rather than silently counting as zero exposure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tradehub_research.db import normalize_ts
from tradehub_research.portfolio.policy import PolicySpec
from tradehub_research.portfolio.prices import (
    annualize,
    average_dollar_volume,
    paired_correlation,
    sample_volatility,
    total_return_series,
)
from tradehub_research.portfolio.snapshot import PortfolioSnapshot
from tradehub_research.portfolio.types import Action, State, json_roundtrip

KNOWN = "KNOWN"


@dataclass(frozen=True)
class RiskInputs:
    """Typed inputs the risk engine needs for one security decision.

    ``direction`` is the PROPOSED action (BUY for ENTER/ADD, SELL for
    TRIM/EXIT); it is set from eligibility, never from the current state
    (a HOLD security can be evaluated for either direction).  Every
    portfolio status (cash/NAV/holdings/valuation/quantity/sellable/
    liquidity) is carried through so UNKNOWN/STALE can never silently
    become trusted.
    """

    security_id: str
    sector: str | None
    sector_coverage_status: str | None
    current_state: State
    position_present: bool
    trusted_quantity_microunits: int | None
    quantity_status: str = KNOWN
    sellable_quantity_microunits: int | None = None
    sellable_status: str = KNOWN
    mark_price_microusd: int | None = None
    price_status: str = KNOWN
    price_as_of: str | None = None
    adv_microusd: int | None = None
    liquidity_status: str = KNOWN
    liquidity_as_of: str | None = None
    nav_microusd: int | None = None
    nav_status: str = KNOWN
    cash_microusd: int | None = None
    cash_status: str = KNOWN
    holdings_status: str = KNOWN
    holding_valuation_status: str = KNOWN
    current_weight_ppm: int = 0
    direction: Action | None = None


@dataclass(frozen=True)
class RiskResult:
    status: str  # PASS | LIMITED | UNKNOWN | BLOCKED | NOT_RUN
    clips: dict[str, int] = field(default_factory=dict)
    measures: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    series_dates: list[str] = field(default_factory=list)
    returns_ppm: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "clips": dict(self.clips),
            "measures": dict(self.measures),
            "reasons": list(self.reasons),
            "evidence_ids": list(self.evidence_ids),
            "series_dates": list(self.series_dates),
            "returns_ppm": list(self.returns_ppm),
        }


def _ppm_from_decimal(value: Decimal) -> int:
    return int((value * Decimal(1_000_000)).to_integral_value())


class RiskEngine:
    def __init__(self, database: Any, policy: PolicySpec, snapshot: PortfolioSnapshot):
        # ``database`` may be a live transaction connection (engine runs) or a
        # ResearchDB (unit tests); both expose the same row interface.
        self.database = database
        self.policy = policy
        self.snapshot = snapshot
        self.risk = policy.risk
        self._series_cache: dict[str, Any] = {}

    def _query(self):
        """Context manager yielding a connection for price queries.

        A plain (read-write) connection is required: ``mode=ro`` URI
        connections cannot see uncheckpointed WAL frames while another
        connection holds an open write transaction, which would make the
        ledger appear empty mid-run.
        """
        from contextlib import nullcontext

        if hasattr(self.database, "connect"):
            return self.database.connect()
        return nullcontext(self.database)

    # -- history measures ---------------------------------------------------

    def _series(self, security_id: str, as_of: str) -> Any:
        key = f"{security_id}@{as_of}"
        if key not in self._series_cache:
            if hasattr(self.database, "connect"):
                with self.database.connect() as conn:
                    self._series_cache[key] = total_return_series(conn, security_id, as_of)
            else:
                self._series_cache[key] = total_return_series(self.database, security_id, as_of)
        return self._series_cache[key]

    def _history_measures(self, security_id: str, as_of: str) -> tuple[dict[str, Any], list[str]]:
        risk = self.risk
        series = self._series(security_id, as_of)
        measures: dict[str, Any] = {}
        evidence_ids: list[str] = list(series.evidence_ids)
        volatility = sample_volatility(
            series,
            int(risk["volatility_window_sessions"]),
            int(risk["min_vol_observations"]),
        )
        if volatility is not None:
            measures["annualized_vol_ppm"] = _ppm_from_decimal(
                annualize(volatility, int(risk["annualization_sessions"]))
            )
        else:
            measures["annualized_vol_ppm"] = None
        with self._query() as query_conn:
            adv = average_dollar_volume(
                query_conn,
                security_id,
                as_of,
                int(risk["adv_window_sessions"]),
                int(risk["min_adv_observations"]),
            )
        measures["ledger_adv_microusd"] = adv
        measures["return_count"] = len(series)
        return measures, evidence_ids

    def _correlation_with_holdings(
        self, security_id: str, as_of: str
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """Correlation of the candidate against each correlated holding.

        Date-aligned overlapping-return Pearson.  Returns
        ``correlated_holdings``, ``correlated_book_ppm``, and
        ``unassessable_holdings`` — holdings above the material-weight
        threshold whose correlation could not be computed (insufficient
        overlap, zero variance, or missing history).  Fail-closed callers
        treat unassessable holdings as UNKNOWN, never as zero exposure.
        """
        risk = self.risk
        candidate_series = self._series(security_id, as_of)
        correlated: list[dict[str, Any]] = []
        unassessable: list[dict[str, Any]] = []
        correlated_book = 0
        threshold = int(risk["correlation_threshold_ppm"])
        min_holding_weight = int(risk["min_correlated_holding_ppm"])
        window = int(risk["correlation_window_sessions"])
        min_overlap = int(risk["min_overlap_observations"])
        for holding in self.snapshot.holdings:
            if holding["security_id"] == security_id:
                continue
            if holding["market_value_microusd"] is None or self.snapshot.nav_microusd is None:
                continue
            weight_ppm = round(
                holding["market_value_microusd"] * 1_000_000 / self.snapshot.nav_microusd
            )
            if weight_ppm < min_holding_weight:
                continue
            other_series = self._series(holding["security_id"], as_of)
            correlation = paired_correlation(candidate_series, other_series, window, min_overlap)
            if correlation is None:
                unassessable.append(
                    {
                        "security_id": holding["security_id"],
                        "weight_ppm": weight_ppm,
                        "reason": "correlation_unassessable",
                    }
                )
                continue
            correlation_ppm = _ppm_from_decimal(correlation)
            if correlation_ppm >= threshold:
                correlated.append(
                    {
                        "security_id": holding["security_id"],
                        "weight_ppm": weight_ppm,
                        "correlation_ppm": correlation_ppm,
                    }
                )
                correlated_book += weight_ppm
        return (
            {
                "correlated_holdings": correlated,
                "correlated_book_ppm": correlated_book,
                "unassessable_holdings": unassessable,
            },
            [],
            [h["security_id"] for h in unassessable],
        )

    # -- composition ----------------------------------------------------------

    def evaluate(self, inputs: RiskInputs, as_of: str) -> RiskResult:
        """Run risk checks for one decision; compose UNKNOWN per fail-closed rules."""
        risk = self.risk
        reasons: list[str] = []
        clips: dict[str, int] = {}
        measures: dict[str, Any] = {}
        evidence_ids: list[str] = []
        status = "NOT_RUN"

        is_buy = inputs.direction == Action.BUY
        is_sell = inputs.direction == Action.SELL

        # Factor / drawdown seams: honest NOT_AVAILABLE, block when required.
        if risk["factor_required"]:
            return RiskResult("BLOCKED", reasons=["factor_seam_required_unavailable"])
        if risk["drawdown_required"]:
            return RiskResult("BLOCKED", reasons=["drawdown_seam_required_unavailable"])
        measures["factor_available"] = False
        measures["drawdown_available"] = False

        history_measures, history_ids = self._history_measures(inputs.security_id, as_of)
        measures.update(history_measures)
        evidence_ids.extend(history_ids)

        if is_buy:
            # Every critical input must be trusted and current for an increase.
            if inputs.nav_microusd is None or inputs.nav_status != KNOWN:
                reasons.append("nav_unknown")
            if inputs.cash_microusd is None or inputs.cash_status != KNOWN:
                reasons.append("cash_unknown")
            if inputs.sector is None:
                reasons.append("sector_unknown")
            if inputs.holdings_status != KNOWN:
                reasons.append("holdings_unknown")
            if inputs.mark_price_microusd is None or inputs.price_status == "UNKNOWN":
                reasons.append("mark_unknown")
            elif inputs.price_status == "STALE":
                reasons.append("mark_stale")
            if (
                inputs.adv_microusd is None
                or inputs.liquidity_status == "UNKNOWN"
                or inputs.liquidity_as_of is None
            ):
                reasons.append("liquidity_unknown")
            elif inputs.liquidity_status == "STALE":
                reasons.append("liquidity_stale")
            if inputs.position_present and (
                inputs.trusted_quantity_microunits is None or inputs.quantity_status != KNOWN
            ):
                reasons.append("quantity_unknown")
            if measures.get("annualized_vol_ppm") is None:
                reasons.append("volatility_unknown")
            if int(measures.get("return_count", 0)) < int(risk["min_return_observations"]):
                reasons.append("return_history_insufficient")
            if reasons:
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            if _is_stale(inputs.price_as_of, as_of, int(risk["price_stale_calendar_days"])):
                reasons.append("mark_stale")
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            if _is_stale(inputs.liquidity_as_of, as_of, int(risk["price_stale_calendar_days"])):
                reasons.append("liquidity_stale")
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            # ledger ADV is authoritative: missing or contradictory ledger
            # liquidity blocks an increase
            ledger_adv = measures.get("ledger_adv_microusd")
            if ledger_adv is None:
                reasons.append("liquidity_history_insufficient")
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            tolerance = int(risk["snapshot_tolerance_ppm"])
            if tolerance > 0 and inputs.adv_microusd is not None:
                if ledger_adv == 0:
                    reasons.append("liquidity_reconciliation_failed")
                    return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
                divergence = abs(inputs.adv_microusd - ledger_adv) * 1_000_000 // ledger_adv
                if divergence > tolerance:
                    reasons.append("liquidity_reconciliation_failed")
                    return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            status = "PASS"
            # --- clips (target exposure reductions, not hard blocks) ----
            nav = inputs.nav_microusd
            clips["position"] = int(risk["max_position_ppm"])
            clips["sector"] = inputs.current_weight_ppm + max(
                0,
                int(risk["max_sector_ppm"]) - self.snapshot.sector_total_ppm(inputs.sector),
            )
            clips["book"] = inputs.current_weight_ppm + max(
                0,
                int(risk["max_active_signal_book_ppm"]) - self._active_book_ppm(inputs.security_id),
            )
            correlation_measures, _, unassessable = self._correlation_with_holdings(
                inputs.security_id, as_of
            )
            measures.update(correlation_measures)
            if unassessable:
                # correlation is fail-closed for increases
                reasons.append("correlation_unassessable")
                measures["correlation_unassessable"] = sorted(unassessable)
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            clips["correlation"] = max(
                0,
                int(risk["max_correlated_book_ppm"])
                - int(correlation_measures.get("correlated_book_ppm", 0)),
            )
            realized_vol = measures.get("annualized_vol_ppm")
            if realized_vol is not None and realized_vol > int(risk["max_annualized_vol_ppm"]):
                clips["volatility"] = max(
                    0,
                    int(
                        int(risk["max_position_ppm"])
                        * int(risk["max_annualized_vol_ppm"])
                        // realized_vol
                    ),
                )
            else:
                clips["volatility"] = int(risk["max_position_ppm"])
            if inputs.adv_microusd is not None:
                clips["liquidity"] = max(
                    0,
                    int(inputs.adv_microusd * int(risk["max_position_adv_days_ppm"]) // nav),
                )
            else:
                clips["liquidity"] = int(risk["max_position_ppm"])
            return RiskResult(
                status, clips=clips, measures=measures, reasons=reasons, evidence_ids=evidence_ids
            )

        if is_sell:
            # SELL requires trusted quantity/sellable/valuation/NAV/liquidity.
            if inputs.trusted_quantity_microunits is None or inputs.quantity_status != KNOWN:
                reasons.append("quantity_unknown")
            if inputs.sellable_quantity_microunits is None or inputs.sellable_status != KNOWN:
                reasons.append("sellable_unknown")
            if inputs.mark_price_microusd is None or inputs.price_status == "UNKNOWN":
                reasons.append("mark_unknown")
            elif inputs.price_status == "STALE":
                reasons.append("mark_stale")
            if inputs.holding_valuation_status != KNOWN:
                reasons.append("valuation_unknown")
            if (
                inputs.adv_microusd is None
                or inputs.liquidity_status == "UNKNOWN"
                or inputs.liquidity_as_of is None
            ):
                reasons.append("liquidity_unknown")
            elif inputs.liquidity_status == "STALE":
                reasons.append("liquidity_stale")
            if inputs.nav_microusd is None or inputs.nav_status != KNOWN:
                reasons.append("nav_unknown")
            if reasons:
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            if _is_stale(inputs.price_as_of, as_of, int(risk["price_stale_calendar_days"])):
                reasons.append("mark_stale")
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            if _is_stale(inputs.liquidity_as_of, as_of, int(risk["price_stale_calendar_days"])):
                reasons.append("liquidity_stale")
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            # Exposure decreases: unknown measures follow the policy's
            # unknown_decrease composition (LIMITED at most, or BLOCK).
            status = "PASS"
            unknown_measures: list[str] = []
            if measures.get("annualized_vol_ppm") is None:
                unknown_measures.append("volatility_unknown")
            correlation_measures, _, _ = self._correlation_with_holdings(inputs.security_id, as_of)
            measures.update(correlation_measures)
            if correlation_measures.get("unassessable_holdings"):
                unknown_measures.append("correlation_unassessable")
            if unknown_measures:
                reasons.extend(unknown_measures)
                if risk["unknown_decrease"] == "BLOCK":
                    return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
                status = "LIMITED"
            clips["sellable"] = int(inputs.sellable_quantity_microunits)
            return RiskResult(
                status, clips=clips, measures=measures, reasons=reasons, evidence_ids=evidence_ids
            )

        # Non-action decisions: risk not required.
        return RiskResult("NOT_RUN", measures=measures, evidence_ids=evidence_ids)

    def _active_book_ppm(self, security_id: str) -> int:
        """Sum of weights of ALL other holdings (snapshot-complete, conservative).

        Deliberately not restricted to pending/actionable states: the snapshot
        has no per-holding state field, so the full book is the honest upper
        bound on active exposure — conservative in the block direction.
        """
        total = 0
        for holding in self.snapshot.holdings:
            if holding["security_id"] == security_id:
                continue
            if holding["market_value_microusd"] is None or self.snapshot.nav_microusd is None:
                continue
            total += round(
                holding["market_value_microusd"] * 1_000_000 / self.snapshot.nav_microusd
            )
        return total


def _is_stale(price_as_of: str | None, as_of: str, max_days: int) -> bool:
    if price_as_of is None:
        return True
    try:
        from datetime import datetime

        price_time = datetime.fromisoformat(normalize_ts(price_as_of).replace("Z", "+00:00"))
        moment = datetime.fromisoformat(normalize_ts(as_of).replace("Z", "+00:00"))
    except ValueError:
        return True
    age = moment - price_time
    if age.days < 0:
        return True  # a future timestamp is never fresh
    return age.days > max_days


def encode_risk_json(result: RiskResult) -> str:
    return json_roundtrip(result.as_dict())
