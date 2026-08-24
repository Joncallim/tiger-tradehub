"""event / event_corporate_action_trigger / 1 (design section 3.5).

Deliberately an identity-risk research trigger, not a broad catalyst detector.
Pass when an active, non-superseded identity event (delisting, ticker_change,
share_class_change) became public inside the lookback window.  Delisting is an
``adverse_event``; the other two are ``structural_event``.  A complete identity
feed with no event is a sufficient negative; an incomplete feed is
insufficient.  Splits, dividends, mergers, spin-offs and tender offers do not
pass v1.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from tradehub_research.hunters.common import (
    as_date,
    feature_value,
    insufficient,
    negative,
    parse_ts,
    positive,
)
from tradehub_research.screens import ScreenContext, ScreenResultPayload, ScreenSpec, SecurityId

SCREEN_SPEC = ScreenSpec(
    family="event",
    screen_id="event_corporate_action_trigger",
    screen_version=1,
    feature_schema_version=1,
    parameters={
        "lookback_days": 30,
        "supported_event_types": ["delisting", "ticker_change", "share_class_change"],
    },
    required_features=["identity_events", "identity_feed_complete"],
    implementation_id="hunters/event.py:v1",
)


def _active_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    superseded = {row.get("supersedes_id") for row in rows if row.get("supersedes_id")}
    return [row for row in rows if row.get("id") not in superseded]


def _features(events: list[dict[str, Any]], feed_complete: bool, window: tuple[str, str]):
    sources = [
        {
            "value": event.get("event_type"),
            "unit": "identity_event",
            "role": "identity_event",
            "evidence_id": f"identity-{event.get('id')}",
            "public_available_time": event.get("public_available_time"),
        }
        for event in events
    ]
    return {
        "identity_events_in_window": feature_value(len(events), "count", sources),
        "identity_feed_complete": feature_value(feed_complete, "bool", []),
        "window": feature_value({"start": window[0], "end": window[1]}, "date_range", []),
    }


def evaluate(context: ScreenContext, security_id: SecurityId) -> ScreenResultPayload:
    as_of = parse_ts(context.as_of)
    params = SCREEN_SPEC.parameters
    lookback = int(params["lookback_days"])
    supported = set(params["supported_event_types"])
    window_start = as_of.date() - timedelta(days=lookback)
    window = (window_start.isoformat(), as_of.date().isoformat())

    feed_state = context.identity_feed_complete
    feed_complete = (
        bool(feed_state.get(security_id, False))
        if isinstance(feed_state, dict) or hasattr(feed_state, "get")
        else bool(feed_state)
    )
    events = _active_events(context.identity_events.get(security_id, []))
    in_window = [
        event
        for event in events
        if event.get("event_type") in supported
        and event.get("public_available_time")
        and window_start <= as_date(event["public_available_time"]) <= as_of.date()
    ]
    in_window.sort(key=lambda e: (e.get("public_available_time") or "", e.get("id") or 0))

    features = _features(in_window, feed_complete, window)

    if not feed_complete:
        # Inability to prove completeness makes the screen insufficient (design 4).
        return insufficient(["incomplete_identity_feed"], features, 0.0)

    if not in_window:
        # Complete feed with no event is a sufficient negative.
        return negative(["no_identity_event"], features, 1.0, 1.0)

    reason_codes = sorted(
        {
            "adverse_event" if event["event_type"] == "delisting" else "structural_event"
            for event in in_window
        }
    )
    return positive(reason_codes, features, 1.0, 1.0)
