from __future__ import annotations

import json
import sqlite3
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

    def __init__(
        self, hourly_limit: int = 50, daily_limit: int = 1000, *, state_path: Path | None = None
    ):
        self.hourly_limit, self.daily_limit = hourly_limit, daily_limit
        self.events: deque[float] = deque()
        self.state_path = state_path

    def _connect(self):
        assert self.state_path is not None
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.state_path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("CREATE TABLE IF NOT EXISTS request_event(requested_at REAL NOT NULL)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS bootstrap_symbol("
            "symbol TEXT PRIMARY KEY, first_requested_at REAL NOT NULL)"
        )
        return db

    def acquire(self, now: float) -> None:
        if self.state_path is not None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM request_event WHERE requested_at <= ?", (now - 86400,))
                hourly = db.execute(
                    "SELECT COUNT(*) FROM request_event WHERE requested_at > ?", (now - 3600,)
                ).fetchone()[0]
                daily = db.execute("SELECT COUNT(*) FROM request_event").fetchone()[0]
                if hourly >= int(self.hourly_limit * 0.9) or daily >= int(self.daily_limit * 0.9):
                    raise RuntimeError("Tiingo quota reserve reached; ingestion failed closed")
                db.execute("INSERT INTO request_event VALUES (?)", (now,))
            return
        while self.events and self.events[0] <= now - 86400:
            self.events.popleft()
        hourly = sum(item > now - 3600 for item in self.events)
        if hourly >= int(self.hourly_limit * 0.9) or len(self.events) >= int(
            self.daily_limit * 0.9
        ):
            raise RuntimeError("Tiingo quota reserve reached; ingestion failed closed")
        self.events.append(now)

    def remaining(self, now: float) -> dict[str, int]:
        if self.state_path is not None:
            with self._connect() as db:
                hourly = db.execute(
                    "SELECT COUNT(*) FROM request_event WHERE requested_at > ?", (now - 3600,)
                ).fetchone()[0]
                daily = db.execute(
                    "SELECT COUNT(*) FROM request_event WHERE requested_at > ?", (now - 86400,)
                ).fetchone()[0]
            return {
                "hourly": int(self.hourly_limit * 0.9) - hourly,
                "daily": int(self.daily_limit * 0.9) - daily,
            }
        while self.events and self.events[0] <= now - 86400:
            self.events.popleft()
        hourly = sum(item > now - 3600 for item in self.events)
        return {
            "hourly": int(self.hourly_limit * 0.9) - hourly,
            "daily": int(self.daily_limit * 0.9) - len(self.events),
        }

    def reserve_bootstrap_symbol(self, symbol: str, now: float, limit: int = 450) -> None:
        """Reserve one case-normalized symbol in the durable rolling-month set."""
        symbol = symbol.upper()
        if self.state_path is None:
            return
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM bootstrap_symbol WHERE first_requested_at <= ?", (now - 30 * 86400,)
            )
            if db.execute("SELECT 1 FROM bootstrap_symbol WHERE symbol=?", (symbol,)).fetchone():
                return
            count = db.execute("SELECT COUNT(*) FROM bootstrap_symbol").fetchone()[0]
            if count >= limit:
                raise RuntimeError("Tiingo 450-symbol rolling-month bootstrap ceiling reached")
            db.execute("INSERT INTO bootstrap_symbol VALUES (?,?)", (symbol, now))


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
        self.quota = quota or TiingoQuota(state_path=cache_dir / "tiingo-operational.sqlite")
        super().__init__(
            user_agent=user_agent,
            cache_dir=cache_dir,
            before_attempt=lambda: self.quota.acquire(datetime.now(timezone.utc).timestamp()),
            **kwargs,
        )
        self._token = token

    def fetch_prices(self, ticker: str, start_date: str, end_date: str) -> FetchResult:
        self.quota.reserve_bootstrap_symbol(ticker, datetime.now(timezone.utc).timestamp())
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
                    "factor" if record_type == "split" else "cash": float(value),
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
