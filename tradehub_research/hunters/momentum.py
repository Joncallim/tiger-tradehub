"""momentum_confirmation / momentum_trend_confirmation / 1 (design section 3.6).

Pass iff the 126-day skipped return, the distance to the 252-day moving
average, and the 20-day average dollar volume all meet their floors, over at
least 260 eligible raw daily bars.  Total return and the moving average are
computed from query-time as-of-adjusted closes (eligible actions only); ADV
uses raw close x raw volume.  Momentum never creates eligibility — it is a
tie-break/pass flag only.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.hunters.common import (
    adjusted_close_series,
    as_date,
    eligible_actions,
    eligible_bars,
    feature_value,
    insufficient,
    negative,
    parse_ts,
    positive,
    trading_days_since,
)
from tradehub_research.screens import ScreenContext, ScreenResultPayload, ScreenSpec, SecurityId

SCREEN_SPEC = ScreenSpec(
    family="momentum_confirmation",
    screen_id="momentum_trend_confirmation",
    screen_version=1,
    feature_schema_version=1,
    parameters={
        "return_lookback_bars": 126,
        "return_skip_latest_bars": 5,
        "moving_average_bars": 252,
        "adv_bars": 20,
        "min_return_126d": 0.0,
        "min_ma_distance": 0.0,
        "min_adv": 5000000.0,
        "required_bars": 260,
        "max_bar_age_trading_days": 3,
    },
    required_features=["price_bars", "corporate_actions"],
    implementation_id="hunters/momentum.py:v1",
)


def _features(bar_count: int) -> dict[str, Any]:
    return {
        "eligible_bar_count": feature_value(bar_count, "bars", []),
        "return_126d": feature_value(None, "ratio", []),
        "ma_distance_252d": feature_value(None, "ratio", []),
        "adv_20d": feature_value(None, "usd", []),
    }


def evaluate(context: ScreenContext, security_id: SecurityId) -> ScreenResultPayload:
    as_of = parse_ts(context.as_of)
    params = SCREEN_SPEC.parameters

    bars = eligible_bars(context, security_id, as_of)
    features = _features(len(bars))
    bar_sources = [
        {
            "value": bar.get("close"),
            "unit": "usd_per_share",
            "session_date": bar.get("session_date"),
            "role": "price_bar",
            "evidence_id": bar.get("evidence_id"),
        }
        for bar in bars
    ]
    features["eligible_bar_count"] = feature_value(len(bars), "bars", bar_sources)

    if len(bars) < int(params["required_bars"]):
        return insufficient(["insufficient_price_history"], features, 0.0)

    latest = bars[-1]
    if trading_days_since(as_date(latest["session_date"]), as_of) > int(
        params["max_bar_age_trading_days"]
    ):
        return insufficient(["stale_price_bar"], features, 0.0)

    actions = eligible_actions(context, security_id, as_of)
    action_sources = [
        {
            "value": action.get("factor", action.get("cash")),
            "unit": "action",
            "effective_date": action.get("effective_date"),
            "role": action.get("action_type"),
            "evidence_id": action.get("evidence_id"),
        }
        for action in actions
    ]
    calculation_sources = bar_sources + action_sources
    adjusted = adjusted_close_series(bars, actions)
    if len(adjusted) != len(bars):
        return insufficient(["insufficient_price_history"], features, 0.0)

    lookback = int(params["return_lookback_bars"])
    skip = int(params["return_skip_latest_bars"])
    ma_bars = int(params["moving_average_bars"])
    adv_bars = int(params["adv_bars"])

    if len(adjusted) < lookback + skip + 1 or len(adjusted) < ma_bars:
        return insufficient(["insufficient_price_history"], features, 0.0)

    skipped_end = len(adjusted) - skip  # skip the most recent `skip` bars
    end_close = adjusted[skipped_end - 1][1]
    start_close = adjusted[skipped_end - 1 - lookback][1]
    if start_close <= 0:
        return negative(["nonpositive_base_price"], features, 1.0, 0.5)
    return_126d = end_close / start_close - 1.0

    ma_window = [close for _day, close in adjusted[-ma_bars:]]
    moving_average = sum(ma_window) / len(ma_window)
    latest_close = adjusted[-1][1]
    ma_distance = (latest_close / moving_average - 1.0) if moving_average > 0 else None

    adv_window = bars[-adv_bars:]
    adv = sum(float(b["close"]) * float(b["volume"]) for b in adv_window) / len(adv_window)

    features["return_126d"] = feature_value(round(return_126d, 6), "ratio", calculation_sources)
    features["ma_distance_252d"] = feature_value(
        None if ma_distance is None else round(ma_distance, 6), "ratio", calculation_sources
    )
    features["adv_20d"] = feature_value(round(adv, 2), "usd", bar_sources)

    if ma_distance is None:
        return insufficient(["noncomparable_moving_average"], features, 0.0)

    passed = (
        return_126d >= float(params["min_return_126d"])
        and ma_distance >= float(params["min_ma_distance"])
        and adv >= float(params["min_adv"])
    )
    if passed:
        return positive(["momentum_floors_met"], features, 1.0, 1.0)
    return negative(["momentum_floors_not_met"], features, 1.0, 1.0)


_ = Any  # typing placeholder retained for future strict typing
