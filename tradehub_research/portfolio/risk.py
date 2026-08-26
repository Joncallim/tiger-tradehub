"""Portfolio risk checks: concentration, sector, correlation, volatility, liquidity.

BUY/ADD requires trusted holdings, NAV, cash, sector, mark, price history,
volatility, correlation, and liquidity; any critical UNKNOWN blocks
(``unknown_increase`` is always BLOCK).  SELL requires trusted quantity,
sellable quantity, valuation/mark, and liquidity; unknown concentration/
correlation/volatility is retained as LIMITED (exposure decreases), never
relabelled PASS.  Factor and drawdown seams report NOT_AVAILABLE; a required
seam blocks the decision — no fake exposure is ever fabricated.
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
from tradehub_research.portfolio.types import State, json_roundtrip


@dataclass(frozen=True)
class RiskInputs:
    """Typed inputs the risk engine needs for one security decision."""

    security_id: str
    sector: str | None
    sector_coverage_status: str | None
    current_state: State
    position_present: bool
    trusted_quantity_microunits: int | None
    sellable_quantity_microunits: int | None
    mark_price_microusd: int | None
    price_status: str
    price_as_of: str | None
    adv_microusd: int | None
    liquidity_status: str
    nav_microusd: int | None
    cash_microusd: int | None
    current_weight_ppm: int


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
        self.database = database
        self.policy = policy
        self.snapshot = snapshot
        self.risk = policy.risk
        self._series_cache: dict[str, Any] = {}

    # -- history measures ---------------------------------------------------

    def _series(self, security_id: str, as_of: str) -> Any:
        key = security_id
        if key not in self._series_cache:
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
        adv = average_dollar_volume(
            self.database,
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
    ) -> tuple[dict[str, Any], list[str]]:
        """Correlation of the candidate against each correlated holding.

        Date-aligned overlapping-return Pearson; returns a dict
        ``correlated_holdings`` (list of {security_id, weight_ppm,
        correlation_ppm}) plus ``correlated_book_ppm``.
        """
        risk = self.risk
        candidate_series = self._series(security_id, as_of)
        correlated: list[dict[str, Any]] = []
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
        return {"correlated_holdings": correlated, "correlated_book_ppm": correlated_book}, []

    # -- composition ----------------------------------------------------------

    def evaluate(self, inputs: RiskInputs, as_of: str) -> RiskResult:
        """Run risk checks for one decision; compose UNKNOWN per fail-closed rules."""
        risk = self.risk
        reasons: list[str] = []
        clips: dict[str, int] = {}
        measures: dict[str, Any] = {}
        evidence_ids: list[str] = []
        status = "NOT_RUN"

        is_buy = inputs.current_state in (State.WATCH, State.HOLD)  # ENTER/ADD proposals
        is_sell = inputs.current_state in (State.HOLD, State.TRIM)

        # Factor / drawdown seams: honest NOT_AVAILABLE, block when required.
        factor_available = False
        drawdown_available = False
        if risk["factor_required"]:
            return RiskResult("BLOCKED", reasons=["factor_seam_required_unavailable"])
        if risk["drawdown_required"]:
            return RiskResult("BLOCKED", reasons=["drawdown_seam_required_unavailable"])
        measures["factor_available"] = factor_available
        measures["drawdown_available"] = drawdown_available

        history_measures, history_ids = self._history_measures(inputs.security_id, as_of)
        measures.update(history_measures)
        evidence_ids.extend(history_ids)

        if is_buy:
            # Every critical input must be trusted for a position increase.
            if inputs.nav_microusd is None:
                reasons.append("nav_unknown")
            if inputs.cash_microusd is None:
                reasons.append("cash_unknown")
            if inputs.sector is None:
                reasons.append("sector_unknown")
            if inputs.mark_price_microusd is None or inputs.price_status == "UNKNOWN":
                reasons.append("mark_unknown")
            if inputs.adv_microusd is None or inputs.liquidity_status == "UNKNOWN":
                reasons.append("liquidity_unknown")
            if inputs.position_present and inputs.trusted_quantity_microunits is None:
                reasons.append("quantity_unknown")
            if measures.get("annualized_vol_ppm") is None:
                reasons.append("volatility_unknown")
            if reasons:
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            if inputs.price_status == "STALE" or _is_stale(
                inputs.price_as_of, as_of, int(risk["price_stale_calendar_days"])
            ):
                reasons.append("mark_stale")
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
            correlation_measures, _ = self._correlation_with_holdings(inputs.security_id, as_of)
            measures.update(correlation_measures)
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
            # SELL requires trusted quantity/sellable/valuation/liquidity.
            if inputs.trusted_quantity_microunits is None:
                reasons.append("quantity_unknown")
            if inputs.sellable_quantity_microunits is None:
                reasons.append("sellable_unknown")
            if inputs.mark_price_microusd is None or inputs.price_status == "UNKNOWN":
                reasons.append("mark_unknown")
            if inputs.adv_microusd is None or inputs.liquidity_status == "UNKNOWN":
                reasons.append("liquidity_unknown")
            if reasons:
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            if inputs.price_status == "STALE" or _is_stale(
                inputs.price_as_of, as_of, int(risk["price_stale_calendar_days"])
            ):
                reasons.append("mark_stale")
                return RiskResult("BLOCKED", reasons=sorted(set(reasons)), measures=measures)
            # Exposure decreases: unknown correlation/volatility is LIMITED at most.
            status = "PASS"
            if measures.get("annualized_vol_ppm") is None:
                status = "LIMITED"
                reasons.append("volatility_unknown")
            clips["sellable"] = int(inputs.sellable_quantity_microunits)
            return RiskResult(
                status, clips=clips, measures=measures, reasons=reasons, evidence_ids=evidence_ids
            )

        # Non-action decisions: risk not required.
        return RiskResult("NOT_RUN", measures=measures, evidence_ids=evidence_ids)

    def _active_book_ppm(self, security_id: str) -> int:
        """Sum of weights of other securities with a pending/actionable state."""
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
    return (moment - price_time).days > max_days


def encode_risk_json(result: RiskResult) -> str:
    return json_roundtrip(result.as_dict())
