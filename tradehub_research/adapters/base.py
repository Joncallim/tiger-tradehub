from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import httpx

from tradehub_research.db import normalize_ts, utc_now
from tradehub_research.evidence import EvidenceStore


@dataclass(frozen=True)
class Freshness:
    last_success_at: str | None = None
    max_source_time_seen: str | None = None
    expected_cadence: str | None = None
    lag_seconds: float | None = None
    consecutive_failures: int = 0
    expected_count: int | None = None
    received_count: int | None = None
    state: str = "fresh"


@dataclass(frozen=True)
class Envelope:
    source_id: str
    source_url: str
    source_record_id: str
    retrieved_at: str
    http_status: int
    etag: str | None
    last_modified: str | None
    raw_content_hash: str
    parser_version: str
    event_time: str
    public_available_time: str | None
    pat_provenance: str
    supersedes_source_record_id: str | None = None
    withdrawn: bool = False
    freshness: Freshness = Freshness()


@dataclass(frozen=True)
class FetchResult:
    url: str
    retrieved_at: str
    status: int
    headers: Mapping[str, str]
    raw_bytes: bytes
    cache_path: Path

    @property
    def raw_content_hash(self) -> str:
        return hashlib.sha256(self.raw_bytes).hexdigest()


@dataclass(frozen=True)
class ParsedRecord:
    envelope: Envelope
    security_identifier: str
    identifier_kind: str
    structured_fields: Mapping[str, Any]
    extraction_confidence: float = 1.0


class HttpTransport(Protocol):
    def build_request(self, method: str, url: str, **kwargs: Any) -> httpx.Request: ...

    def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response: ...


class TokenBucket:
    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rate, self.capacity = rate, capacity
        self.clock, self.sleep = clock, sleep
        self.tokens, self.updated = capacity, clock()

    def acquire(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            delay = (1 - self.tokens) / self.rate
            self.sleep(delay)
            self.updated = self.clock()
            self.tokens = 0
        else:
            self.tokens -= 1


class NetworkClient:
    def __init__(
        self,
        *,
        user_agent: str,
        cache_dir: Path,
        transport: HttpTransport | None = None,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        max_attempts: int = 3,
        before_attempt: Callable[[], None] | None = None,
        max_response_bytes: int = 25 * 1024 * 1024,
        cache_budget_bytes: int = 1024 * 1024 * 1024,
    ):
        if not user_agent.strip():
            raise ValueError("a descriptive User-Agent is required")
        self.user_agent, self.cache_dir = user_agent, cache_dir
        self.transport = transport or httpx.Client()
        self.bucket, self.sleep, self.random_value = bucket, sleep, random_value
        self.max_attempts = max_attempts
        self.before_attempt = before_attempt
        self.max_response_bytes = max_response_bytes
        self.cache_budget_bytes = cache_budget_bytes

    def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> FetchResult:
        request_headers = {"User-Agent": self.user_agent, **dict(headers or {})}
        response: httpx.Response | None = None
        for attempt in range(self.max_attempts):
            if self.before_attempt:
                self.before_attempt()
            if self.bucket:
                self.bucket.acquire()
            try:
                request = self.transport.build_request(
                    "GET", url, headers=request_headers, timeout=httpx.Timeout(30, connect=10)
                )
                response = self.transport.send(request, stream=True)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == self.max_attempts - 1:
                    raise
                self.sleep(self.random_value() * min(60, 2**attempt))
                continue
            if response.status_code not in {403, 429} and response.status_code < 500:
                break
            if attempt == self.max_attempts - 1:
                try:
                    response.raise_for_status()
                finally:
                    response.close()
            retry_after = response.headers.get("Retry-After")
            delay = (
                min(60.0, float(retry_after))
                if retry_after and retry_after.isdigit()
                else min(60.0, self.random_value() * (2**attempt))
            )
            response.close()
            self.sleep(delay)
        assert response is not None
        try:
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > self.max_response_bytes:
                raise ValueError("provider response exceeds configured byte ceiling")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise ValueError("provider response exceeds configured byte ceiling")
                chunks.append(chunk)
        finally:
            response.close()
        raw = b"".join(chunks)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.cache_dir / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            used = sum(item.stat().st_size for item in self.cache_dir.rglob("*") if item.is_file())
            if used + len(raw) > self.cache_budget_bytes:
                raise RuntimeError("adapter cache budget exceeded")
            path.write_bytes(raw)
        return FetchResult(
            url, normalize_ts(utc_now()), response.status_code, response.headers, raw, path
        )


def envelope_from_fetch(fetch: FetchResult, **values: Any) -> Envelope:
    return Envelope(
        source_url=fetch.url,
        retrieved_at=fetch.retrieved_at,
        http_status=fetch.status,
        etag=fetch.headers.get("etag"),
        last_modified=fetch.headers.get("last-modified"),
        raw_content_hash=fetch.raw_content_hash,
        **values,
    )


def resolve_security_id(store: EvidenceStore, identifier: str, kind: str) -> str:
    with store.database.connect(read_only=True) as db:
        if kind == "security_id":
            row = db.execute(
                "SELECT security_id FROM security WHERE security_id=?", (identifier,)
            ).fetchone()
        elif kind == "ticker":
            row = db.execute(
                "SELECT security_id FROM security WHERE upper(canonical_ticker)=upper(?)",
                (identifier,),
            ).fetchone()
        else:
            raise ValueError(f"unsupported security identifier kind: {kind}")
    if row is None:
        raise ValueError(f"security identity is unresolved: {kind}={identifier}")
    return str(row[0])


def ingest_records(
    records: Iterable[ParsedRecord], store: EvidenceStore, *, dry_run: bool = False
) -> list[str]:
    records = list(records)
    if dry_run:
        for record in records:
            resolve_security_id(store, record.security_identifier, record.identifier_kind)
        return [record.envelope.source_record_id for record in records]
    source_defaults = {
        "sec_index": ("regulatory_index", "derived_from_index"),
        "sec_xbrl": ("regulatory_filing", "source_reported"),
        "sec_form4": ("regulatory_filing", "source_reported"),
        "tiingo_eod": ("market_data", "derived_from_index"),
    }
    with store.database.connect() as db:
        for source_id in sorted({record.envelope.source_id for record in records}):
            source_type, provenance = source_defaults[source_id]
            db.execute(
                "INSERT INTO evidence_source VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                (source_id, source_type, 1, "append-only provider adapter", provenance),
            )
    ids: list[str] = []
    known: dict[tuple[str, str, str], str] = {}
    for record in records:
        envelope = record.envelope
        security_id = resolve_security_id(store, record.security_identifier, record.identifier_kind)
        predecessor = None
        if envelope.supersedes_source_record_id:
            key = (envelope.source_id, security_id, envelope.supersedes_source_record_id)
            predecessor = known.get(key)
            if predecessor is None:
                with store.database.connect(read_only=True) as db:
                    row = db.execute(
                        "SELECT evidence_id FROM evidence_event "
                        "WHERE source_id=? AND security_id=? AND source_record_id=?",
                        key,
                    ).fetchone()
                    predecessor = str(row[0]) if row else None
            if predecessor is None:
                raise ValueError("superseded source record has not been ingested")
        fields = dict(record.structured_fields)
        if not envelope.withdrawn:
            fields["source_envelope"] = {
                "source_url": envelope.source_url,
                "retrieved_at": envelope.retrieved_at,
                "http_status": envelope.http_status,
                "etag": envelope.etag,
                "last_modified": envelope.last_modified,
                "raw_content_hash": envelope.raw_content_hash,
                "parser_version": envelope.parser_version,
                "freshness": envelope.freshness.__dict__,
            }
        evidence_id = store.insert(
            security_id=security_id,
            source_id=envelope.source_id,
            source_record_id=envelope.source_record_id,
            structured_fields=fields,
            extraction_confidence=record.extraction_confidence,
            event_time=envelope.event_time,
            public_available_time=envelope.public_available_time,
            pat_provenance=envelope.pat_provenance,
            ingested_time=envelope.retrieved_at,
            supersedes_evidence_id=predecessor,
            withdrawn=envelope.withdrawn,
        )
        known[(envelope.source_id, security_id, envelope.source_record_id)] = evidence_id
        ids.append(evidence_id)
    return ids


def with_supersession(record: ParsedRecord, predecessor: str) -> ParsedRecord:
    return replace(
        record, envelope=replace(record.envelope, supersedes_source_record_id=predecessor)
    )


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()
