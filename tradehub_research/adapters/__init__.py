"""Operational data-ingestion adapters (kept separate from screening)."""

from tradehub_research.adapters.base import Envelope, FetchResult, ParsedRecord, ingest_records
from tradehub_research.adapters.sec import SecAdapter
from tradehub_research.adapters.tiingo import TiingoEodAdapter

__all__ = [
    "Envelope",
    "FetchResult",
    "ParsedRecord",
    "SecAdapter",
    "TiingoEodAdapter",
    "ingest_records",
]
