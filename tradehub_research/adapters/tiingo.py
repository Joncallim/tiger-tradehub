from __future__ import annotations

import json
from collections import deque
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from tradehub_research.adapters.base import (
    FetchResult,
    Freshness,
    NetworkClient,
    ParsedRecord,
    canonical_hash,
    envelope_from_fetch,
)

PARSER_VERSION = "tiingo-eod-v1"


class TiingoQuota:
    """Rolling hourly/daily counters retaining a 10% operator reserve."""

    def __init__(self, hourly_limit: int = 50, daily_limit: int = 1000):
        self.hourly_limit, self.daily_limit = hourly_limit, daily_limit
        self.events: deque[float] = deque()

    def acquire(self, now: float) -> None:
        while self.events and self.events[0] <= now - 86400:
            self.events.popleft()
        hourly = sum(item > now - 3600 for item in self.events)
        if hourly >= int(self.hourly_limit * 0.9) or len(self.events) >= int(
            self.daily_limit * 0.9
        ):
            raise RuntimeError("Tiingo quota reserve reached; ingestion failed closed")
        self.events.append(now)

    def remaining(self, now: float) -> dict[str, int]:
        while self.events and self.events[0] <= now - 86400:
            self.events.popleft()
        hourly = sum(item > now - 3600 for item in self.events)
        return {
            "hourly": int(self.hourly_limit * 0.9) - hourly,
            "daily": int(self.daily_limit * 0.9) - len(self.events),
        }


class TiingoEodAdapter(NetworkClient):
    def __init__(
        self,
        *,
        token: str | None,
        license_confirmed: bool,
        user_agent: str,
        cache_dir: Path,
        quota: TiingoQuota | None = None,
        **kwargs: Any,
    ):
        if not license_confirmed:
            raise ValueError("Tiingo internal-use license must be explicitly confirmed")
        if not token:
            raise ValueError("Tiingo token is not configured")
        super().__init__(user_agent=user_agent, cache_dir=cache_dir, **kwargs)
        self._token = token
        self.quota = quota or TiingoQuota()

    def fetch_prices(self, ticker: str, start_date: str, end_date: str) -> FetchResult:
        self.quota.acquire(datetime.now(timezone.utc).timestamp())
        query = urlencode({"startDate": start_date, "endDate": end_date, "format": "json"})
        url = f"https://api.tiingo.com/tiingo/daily/{quote(ticker, safe='')}/prices?{query}"
        return self.fetch(url, headers={"Authorization": f"Token {self._token}"})

    def parse(
        self,
        raw: bytes,
        metadata: FetchResult,
        *,
        ticker: str,
        supersedes: dict[tuple[str, str], str] | None = None,
    ) -> list[ParsedRecord]:
        bars = json.loads(raw)
        supersedes = supersedes or {}
        records: list[ParsedRecord] = []
        for bar in bars:
            session = bar["date"][:10]
            local_pat = datetime.combine(
                datetime.fromisoformat(session).date(), time(20, 15), ZoneInfo("America/New_York")
            )
            pat = local_pat.astimezone(timezone.utc).isoformat()
            common = dict(
                parser_version=PARSER_VERSION,
                event_time=session,
                public_available_time=pat,
                pat_provenance="derived_from_index",
                freshness=Freshness(
                    last_success_at=metadata.retrieved_at,
                    max_source_time_seen=session,
                    expected_cadence="US trading session",
                    received_count=1,
                ),
            )
            raw_fields = {key: bar.get(key) for key in ("open", "high", "low", "close", "volume")}
            # Adjusted values are retained only under an explicit audit namespace.
            adjusted_audit = {
                key: bar.get(key)
                for key in ("adjOpen", "adjHigh", "adjLow", "adjClose", "adjVolume")
                if key in bar
            }
            fields = {
                "record_type": "price_bar",
                "provider_ticker": ticker,
                "session_date": session,
                **raw_fields,
                "provider_adjusted_audit_only": adjusted_audit,
            }
            payload_hash = canonical_hash(fields)
            record_id = f"{ticker}:{session}:price_bar:{payload_hash}"
            envelope = envelope_from_fetch(
                metadata,
                source_id="tiingo_eod",
                source_record_id=record_id,
                supersedes_source_record_id=supersedes.get((session, "price_bar")),
                **common,
            )
            records.append(ParsedRecord(envelope, ticker, "ticker", fields))
            actions = (("split", bar.get("splitFactor"), 1), ("dividend", bar.get("divCash"), 0))
            for record_type, value, neutral in actions:
                if value is None or value == neutral:
                    continue
                action = {
                    "record_type": record_type,
                    "provider_ticker": ticker,
                    "effective_date": session,
                    "split_factor" if record_type == "split" else "cash_amount": value,
                }
                action_hash = canonical_hash(action)
                action_id = f"{ticker}:{session}:{record_type}:{action_hash}"
                action_envelope = envelope_from_fetch(
                    metadata,
                    source_id="tiingo_eod",
                    source_record_id=action_id,
                    supersedes_source_record_id=supersedes.get((session, record_type)),
                    **common,
                )
                records.append(ParsedRecord(action_envelope, ticker, "ticker", action))
        return records
