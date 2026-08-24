"""Deterministic contracts shared by all Phase 1 screening implementations.

``canonical_json`` is the one canonical serializer used by screening: UTF-8 JSON
with recursively sorted object keys, no insignificant whitespace, and no NaN or
Infinity values.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

from tradehub_research.db import normalize_ts, utc_now

SecurityId: TypeAlias = str


def canonical_json(value: object) -> str:
    """Return the repository's deterministic RFC 8785-style JSON representation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ScreenSpec:
    family: str
    screen_id: str
    screen_version: int
    feature_schema_version: int
    parameters: dict[str, Any]
    required_features: list[str]
    implementation_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "screen_id": self.screen_id,
            "screen_version": self.screen_version,
            "feature_schema_version": self.feature_schema_version,
            "parameters": self.parameters,
            "required_features": self.required_features,
            "implementation_id": self.implementation_id,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.as_dict())

    @property
    def config_hash(self) -> str:
        return _sha256(self.canonical_json())


@dataclass(frozen=True)
class ScreenResultPayload:
    raw_features: dict[str, Any]
    evidence_ids: list[str]
    reason_codes: list[str]
    sufficient_data: bool
    passed: bool
    confidence: float
    data_quality: float


@dataclass(frozen=True)
class ScreenResult:
    screen_result_id: str
    run_id: str
    security_id: SecurityId
    config_hash: str
    raw_features: dict[str, Any]
    evidence_ids: list[str]
    reason_codes: list[str]
    sufficient_data: bool
    passed: bool
    confidence: float
    data_quality: float
    result_hash: str
    computed_at: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        security_id: SecurityId,
        config_hash: str,
        raw_features: dict[str, Any],
        evidence_ids: Sequence[str],
        reason_codes: Sequence[str],
        sufficient_data: bool,
        passed: bool,
        confidence: float,
        data_quality: float,
        computed_at: str | None = None,
    ) -> ScreenResult:
        if passed and not sufficient_data:
            raise ValueError("a passing result must have sufficient data")
        if not 0 <= confidence <= 1 or not 0 <= data_quality <= 1:
            raise ValueError("confidence and data_quality must be between 0 and 1")
        if not sufficient_data and confidence != 0:
            raise ValueError("an insufficient result must have zero confidence")
        evidence = sorted(set(evidence_ids))
        reasons = sorted(set(reason_codes))
        result_id = screen_result_id(run_id, security_id, config_hash)
        logical = {
            "screen_result_id": result_id,
            "run_id": run_id,
            "security_id": security_id,
            "config_hash": config_hash,
            "raw_features": raw_features,
            "evidence_ids": evidence,
            "reason_codes": reasons,
            "sufficient_data": sufficient_data,
            "passed": passed,
            "confidence": confidence,
            "data_quality": data_quality,
        }
        return cls(
            screen_result_id=result_id,
            run_id=run_id,
            security_id=security_id,
            config_hash=config_hash,
            raw_features=raw_features,
            evidence_ids=evidence,
            reason_codes=reasons,
            sufficient_data=sufficient_data,
            passed=passed,
            confidence=confidence,
            data_quality=data_quality,
            result_hash=_sha256(canonical_json(logical)),
            computed_at=normalize_ts(computed_at or utc_now()),
        )

    def logical_dict(self) -> dict[str, Any]:
        """Return every logical field covered by result_hash (computed_at is excluded)."""
        return {
            "screen_result_id": self.screen_result_id,
            "run_id": self.run_id,
            "security_id": self.security_id,
            "config_hash": self.config_hash,
            "raw_features": self.raw_features,
            "evidence_ids": self.evidence_ids,
            "reason_codes": self.reason_codes,
            "sufficient_data": self.sufficient_data,
            "passed": self.passed,
            "confidence": self.confidence,
            "data_quality": self.data_quality,
        }

    def verify(self) -> None:
        expected_id = screen_result_id(self.run_id, self.security_id, self.config_hash)
        if self.screen_result_id != expected_id:
            raise ValueError("screen_result_id does not match its logical identity")
        if self.result_hash != _sha256(canonical_json(self.logical_dict())):
            raise ValueError("result_hash does not match the logical result")
        if self.evidence_ids != sorted(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be sorted and unique")
        if self.reason_codes != sorted(set(self.reason_codes)):
            raise ValueError("reason_codes must be sorted and unique")
        if self.passed and not self.sufficient_data:
            raise ValueError("a passing result must have sufficient data")
        if not self.sufficient_data and self.confidence != 0:
            raise ValueError("an insufficient result must have zero confidence")
        if not 0 <= self.confidence <= 1 or not 0 <= self.data_quality <= 1:
            raise ValueError("confidence and data_quality must be between 0 and 1")


def screen_result_id(run_id: str, security_id: SecurityId, config_hash: str) -> str:
    material = f"result-v1\0{run_id}\0{security_id}\0{config_hash}"
    return _sha256(material)


@dataclass(frozen=True)
class ScreenContext:
    facts: Mapping[SecurityId, list[dict[str, Any]]]
    price_bars: Mapping[SecurityId, list[dict[str, Any]]]
    form4: Mapping[SecurityId, list[dict[str, Any]]]
    identity_events: Mapping[SecurityId, list[dict[str, Any]]]
    market_caps: Mapping[SecurityId, float | None]
    universe: list[SecurityId]
    as_of: str


HunterFn: TypeAlias = Callable[[ScreenContext, SecurityId], ScreenResultPayload]
SCREEN_REGISTRY: dict[tuple[str, str, int], tuple[ScreenSpec, HunterFn]] = {}


def register_screen(spec: ScreenSpec, fn: HunterFn) -> None:
    key = (spec.family, spec.screen_id, spec.screen_version)
    existing = SCREEN_REGISTRY.get(key)
    if existing is not None and (
        existing[0].config_hash != spec.config_hash or existing[1] is not fn
    ):
        raise ValueError(f"screen registration collision: {key!r}")
    SCREEN_REGISTRY[key] = (spec, fn)


def get_screen(family: str, screen_id: str, screen_version: int) -> tuple[ScreenSpec, HunterFn]:
    return SCREEN_REGISTRY[(family, screen_id, screen_version)]


def registered_screens() -> tuple[tuple[ScreenSpec, HunterFn], ...]:
    return tuple(SCREEN_REGISTRY[key] for key in sorted(SCREEN_REGISTRY))
