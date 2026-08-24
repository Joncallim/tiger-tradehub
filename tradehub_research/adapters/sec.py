from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from tradehub_research.adapters.base import (
    FetchResult,
    Freshness,
    NetworkClient,
    ParsedRecord,
    TokenBucket,
    canonical_hash,
    envelope_from_fetch,
)

SEC_BASE = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
PARSER_VERSION = "sec-v1"
IDENTITY_FORMS = {"8-K", "8-K/A", "8-K12G3", "8-K12G3/A", "15", "15/A", "25", "25/A"}
CONCEPT_ALIASES = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "Revenues",
    ),
    "net_income": ("NetIncomeLoss",),
    "operating_income": ("OperatingIncomeLoss",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capital_expenditure": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "assets": ("Assets",),
    "equity": ("StockholdersEquity",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "shares_outstanding": ("EntityCommonStockSharesOutstanding",),
    "basic_eps": ("EarningsPerShareBasic",),
    "long_term_debt_current": ("LongTermDebtCurrent",),
    "short_term_borrowings": ("ShortTermBorrowings",),
    "long_term_debt_noncurrent": ("LongTermDebtNoncurrent",),
}
_SEC_BUCKET = TokenBucket(2.0)


def _number(value: str | None) -> int | float | None:
    if value is None or not value.strip():
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def _next_day_pat(filed: str) -> str:
    local = datetime.combine(date.fromisoformat(filed) + timedelta(days=1), datetime.min.time())
    return (
        local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(ZoneInfo("UTC")).isoformat()
    )


def _accession(path: str) -> str:
    name = path.rsplit("/", 1)[-1].split(".", 1)[0]
    if len(name) == 18 and name.isdigit():
        return f"{name[:10]}-{name[10:12]}-{name[12:]}"
    return name


class SecAdapter(NetworkClient):
    """SEC EDGAR discovery and deterministic parsers; deliberately excludes 13F."""

    def __init__(self, *, user_agent: str, cache_dir: Path, **kwargs: Any):
        super().__init__(
            user_agent=user_agent,
            cache_dir=cache_dir,
            bucket=kwargs.pop("bucket", _SEC_BUCKET),
            **kwargs,
        )

    @staticmethod
    def daily_index_url(day: date) -> str:
        quarter = (day.month - 1) // 3 + 1
        return (
            f"{SEC_BASE}/Archives/edgar/daily-index/{day.year}/QTR{quarter}/master.{day:%Y%m%d}.idx"
        )

    @staticmethod
    def revisit_days(day: date) -> tuple[date, date]:
        return day - timedelta(days=1), day

    def fetch_daily_index(self, day: date) -> FetchResult:
        return self.fetch(self.daily_index_url(day))

    def fetch_accession(self, archive_path: str) -> FetchResult:
        return self.fetch(f"{SEC_BASE}/Archives/{archive_path.lstrip('/')}")

    def fetch_companyfacts(self, cik: str) -> FetchResult:
        return self.fetch(f"{SEC_DATA}/api/xbrl/companyfacts/CIK{int(cik):010d}.json")

    def parse_daily_index(self, raw: bytes, metadata: FetchResult) -> list[ParsedRecord]:
        rows: list[ParsedRecord] = []
        for line in raw.decode("latin-1").splitlines():
            parts = line.split("|")
            if (
                len(parts) != 5
                or parts[0] == "CIK"
                or parts[2] not in {"4", "4/A", *IDENTITY_FORMS}
            ):
                continue
            cik, company, form, filed, path = parts
            accession = _accession(path)
            fields = {
                "record_type": "sec_index_entry",
                "cik": cik.zfill(10),
                "company_name": company,
                "form": form,
                "filed": filed,
                "archive_path": path,
                "accession": accession,
                "coverage_date": filed,
                "day_precision": True,
            }
            envelope = envelope_from_fetch(
                metadata,
                source_id="sec_index",
                source_record_id=f"{accession}:index",
                parser_version=PARSER_VERSION,
                event_time=filed,
                public_available_time=_next_day_pat(filed),
                pat_provenance="derived_from_index",
                freshness=Freshness(
                    last_success_at=metadata.retrieved_at,
                    max_source_time_seen=filed,
                    expected_cadence="settled SEC business day",
                    received_count=1,
                ),
            )
            rows.append(ParsedRecord(envelope, cik.zfill(10), "cik", fields))
        return rows

    def index_completeness_records(
        self,
        metadata: FetchResult,
        *,
        index_date: str,
        security_ids: list[str],
        identity_feed_scanned: bool = True,
    ) -> list[ParsedRecord]:
        """Emit settled-empty-capable completeness markers for a scanned index.

        The caller supplies the issuer universe covered by the global scan, so
        issuers with no filing still receive an explicit settled marker.
        """
        pat = _next_day_pat(index_date)
        records: list[ParsedRecord] = []
        for security_id in sorted(set(security_ids)):
            kinds = ["form4_index_coverage"]
            if identity_feed_scanned:
                kinds.append("identity_feed_marker")
            for kind in kinds:
                fields = {
                    "record_type": kind,
                    "index_date": index_date,
                    "settled_empty_allowed": True,
                }
                envelope = envelope_from_fetch(
                    metadata,
                    source_id="sec_index",
                    source_record_id=f"{security_id}:{index_date}:{kind}",
                    parser_version=PARSER_VERSION,
                    event_time=index_date,
                    public_available_time=pat,
                    pat_provenance="derived_from_index",
                    freshness=Freshness(
                        last_success_at=metadata.retrieved_at,
                        max_source_time_seen=index_date,
                        expected_cadence="settled SEC business day",
                    ),
                )
                records.append(ParsedRecord(envelope, security_id, "security_id", fields))
        return records

    def parse_companyfacts(
        self,
        raw: bytes,
        metadata: FetchResult,
        acceptance_by_accession: dict[str, str] | None = None,
    ) -> list[ParsedRecord]:
        doc = json.loads(raw)
        cik = str(doc["cik"]).zfill(10)
        acceptance_by_accession = acceptance_by_accession or {}
        records: list[ParsedRecord] = []
        us_gaap = doc.get("facts", {}).get("us-gaap", {})
        for metric, aliases in CONCEPT_ALIASES.items():
            selected = next((tag for tag in aliases if tag in us_gaap), None)
            if selected is None:  # absence is missingness, not a fabricated zero row
                continue
            concept = us_gaap[selected]
            for unit, facts in concept.get("units", {}).items():
                for fact in facts:
                    accession = fact["accn"]
                    event_time = fact.get("end") or fact.get("filed")
                    acceptance = acceptance_by_accession.get(accession)
                    pat = acceptance or _next_day_pat(fact["filed"])
                    provenance = "source_reported" if acceptance else "derived_from_index"
                    dimensions = fact.get("dimensions", {})
                    fields = {
                        "record_type": "xbrl_fact",
                        "metric": metric,
                        "cik": cik,
                        "taxonomy": "us-gaap",
                        "tag": selected,
                        "label": concept.get("label"),
                        "unit": unit,
                        "value": fact.get("val"),
                        "start": fact.get("start"),
                        "end": fact.get("end"),
                        "accession": accession,
                        "form": fact.get("form"),
                        "fy": fact.get("fy"),
                        "fp": fact.get("fp"),
                        "filed": fact.get("filed"),
                        "frame": fact.get("frame"),
                        "dimensions": dimensions,
                        "day_precision": not bool(acceptance),
                    }
                    identity = canonical_hash(
                        [
                            accession,
                            "us-gaap",
                            selected,
                            unit,
                            fact.get("start"),
                            fact.get("end"),
                            dimensions,
                            fact.get("val"),
                        ]
                    )
                    envelope = envelope_from_fetch(
                        metadata,
                        source_id="sec_xbrl",
                        source_record_id=f"{accession}:{identity}",
                        parser_version=PARSER_VERSION,
                        event_time=event_time,
                        public_available_time=pat,
                        pat_provenance=provenance,
                        freshness=Freshness(
                            last_success_at=metadata.retrieved_at, max_source_time_seen=event_time
                        ),
                    )
                    records.append(ParsedRecord(envelope, cik, "cik", fields))
        return records

    def parse_submissions(self, raw: bytes, metadata: FetchResult) -> list[ParsedRecord]:
        """Normalize recent accession/acceptance and issuer identity metadata."""
        doc = json.loads(raw)
        cik = str(doc["cik"]).zfill(10)
        recent = doc.get("filings", {}).get("recent", {})
        keys = (
            "accessionNumber",
            "filingDate",
            "reportDate",
            "acceptanceDateTime",
            "act",
            "form",
            "fileNumber",
            "filmNumber",
            "items",
            "size",
            "isXBRL",
            "isInlineXBRL",
            "primaryDocument",
            "primaryDocDescription",
        )
        records: list[ParsedRecord] = []
        for position, accession in enumerate(recent.get("accessionNumber", [])):
            filing = {key: recent.get(key, [None] * (position + 1))[position] for key in keys}
            form = filing["form"]
            filed = filing["filingDate"]
            acceptance = filing["acceptanceDateTime"]
            fields = {
                "record_type": "sec_accession_metadata",
                "cik": cik,
                "entity_name": doc.get("name"),
                "tickers": doc.get("tickers", []),
                "exchanges": doc.get("exchanges", []),
                "former_names": doc.get("formerNames", []),
                **filing,
                "identity_form": form in IDENTITY_FORMS,
            }
            envelope = envelope_from_fetch(
                metadata,
                source_id="sec_index",
                source_record_id=f"{accession}:metadata",
                parser_version=PARSER_VERSION,
                event_time=filing["reportDate"] or filed,
                public_available_time=acceptance or _next_day_pat(filed),
                pat_provenance="source_reported" if acceptance else "derived_from_index",
                freshness=Freshness(
                    last_success_at=metadata.retrieved_at,
                    max_source_time_seen=filed,
                    expected_cadence="per-CIK filing update",
                ),
            )
            records.append(ParsedRecord(envelope, cik, "cik", fields))
        return records

    def parse_form4(
        self,
        raw: bytes,
        metadata: FetchResult,
        *,
        accession: str,
        filed: str,
        acceptance_time: str | None = None,
        supersedes_accession: str | None = None,
        supersedes_transaction_keys: set[str] | None = None,
    ) -> list[ParsedRecord]:
        root = ET.fromstring(raw)
        get = lambda path: root.findtext(path)  # noqa: E731
        cik = (get("issuer/issuerCik") or "").zfill(10)
        ticker = get("issuer/issuerTradingSymbol")
        owner = root.find("reportingOwner")
        owner_fields = {
            "reporting_owner_cik": owner.findtext("reportingOwnerId/rptOwnerCik")
            if owner is not None
            else None,
            "reporting_owner_name": owner.findtext("reportingOwnerId/rptOwnerName")
            if owner is not None
            else None,
            "is_director": owner.findtext("reportingOwnerRelationship/isDirector")
            if owner is not None
            else None,
            "is_officer": owner.findtext("reportingOwnerRelationship/isOfficer")
            if owner is not None
            else None,
            "is_ten_percent_owner": owner.findtext("reportingOwnerRelationship/isTenPercentOwner")
            if owner is not None
            else None,
            "officer_title": owner.findtext("reportingOwnerRelationship/officerTitle")
            if owner is not None
            else None,
        }
        amended = (get("documentType") or "").endswith("/A") or get("amendmentFlag") == "1"
        # The filing date is supplied by the index/submissions seam.  Falling
        # back to periodOfReport would disclose a filing before it was filed;
        # falling back to today's date would make fixture parsing nondeterministic.
        pat = acceptance_time or _next_day_pat(filed)
        provenance = "source_reported" if acceptance_time else "derived_from_index"
        records: list[ParsedRecord] = []
        paths = (
            ("nonDerivativeTable/nonDerivativeTransaction", False),
            ("derivativeTable/derivativeTransaction", True),
        )
        for table_path, derivative in paths:
            for tx in root.findall(table_path):

                def value(path: str, node: ET.Element = tx) -> str | None:
                    return node.findtext(path + "/value")

                event_time = value("transactionDate") or get("periodOfReport")
                fields = {
                    "record_type": "form4_transaction",
                    "accession": accession,
                    "cik": cik,
                    "issuer_ticker": ticker,
                    **owner_fields,
                    "transaction_date": event_time,
                    "transaction_code": tx.findtext("transactionCoding/transactionCode"),
                    "acquired_disposed": value(
                        "transactionAmounts/transactionAcquiredDisposedCode"
                    ),
                    "shares": _number(value("transactionAmounts/transactionShares")),
                    "price_per_share": _number(
                        value("transactionAmounts/transactionPricePerShare")
                    ),
                    "post_transaction_shares": value(
                        "postTransactionAmounts/sharesOwnedFollowingTransaction"
                    ),
                    "direct_indirect": value("ownershipNature/directOrIndirectOwnership"),
                    "security_title": value("securityTitle"),
                    "derivative": derivative,
                    "amendment": amended,
                    "footnote_ids": [x.attrib.get("id") for x in tx.findall(".//footnoteId")],
                }
                fields["owner_id"] = fields["reporting_owner_cik"]
                transaction_key = canonical_hash(
                    {
                        "derivative": derivative,
                        "owner_id": fields["owner_id"],
                        "security_title": fields["security_title"],
                        "transaction_date": fields["transaction_date"],
                        "transaction_code": fields["transaction_code"],
                        "acquired_disposed": fields["acquired_disposed"],
                        "direct_indirect": fields["direct_indirect"],
                        "footnote_ids": fields["footnote_ids"],
                    }
                )
                source_id = f"{accession}:tx:{transaction_key}"
                predecessor = None
                if (
                    supersedes_accession
                    and supersedes_transaction_keys
                    and transaction_key in supersedes_transaction_keys
                ):
                    predecessor = f"{supersedes_accession}:tx:{transaction_key}"
                envelope = envelope_from_fetch(
                    metadata,
                    source_id="sec_form4",
                    source_record_id=source_id,
                    parser_version=PARSER_VERSION,
                    event_time=event_time,
                    public_available_time=pat,
                    pat_provenance=provenance,
                    supersedes_source_record_id=predecessor,
                    freshness=Freshness(
                        last_success_at=metadata.retrieved_at, max_source_time_seen=event_time
                    ),
                )
                records.append(ParsedRecord(envelope, cik, "cik", fields))
        if supersedes_accession and supersedes_transaction_keys:
            current_keys = {r.envelope.source_record_id.rsplit(":tx:", 1)[1] for r in records}
            for transaction_key in sorted(supersedes_transaction_keys - current_keys):
                predecessor = f"{supersedes_accession}:tx:{transaction_key}"
                envelope = envelope_from_fetch(
                    metadata,
                    source_id="sec_form4",
                    source_record_id=f"{accession}:withdraw:{transaction_key}",
                    parser_version=PARSER_VERSION,
                    event_time=filed,
                    public_available_time=pat,
                    pat_provenance=provenance,
                    supersedes_source_record_id=predecessor,
                    withdrawn=True,
                    freshness=Freshness(
                        last_success_at=metadata.retrieved_at, max_source_time_seen=filed
                    ),
                )
                records.append(ParsedRecord(envelope, cik, "cik", {}))
        return records

    @staticmethod
    def with_security(records: list[ParsedRecord], security_id: str) -> list[ParsedRecord]:
        """Bind SEC CIK records after canonical identity resolution by the caller."""
        return [
            replace(row, security_identifier=security_id, identifier_kind="security_id")
            for row in records
        ]
