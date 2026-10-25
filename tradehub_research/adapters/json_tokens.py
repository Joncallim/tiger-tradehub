"""Pure, inactive preservation of JSON numeric lexemes at a parse boundary.

This helper is intentionally not wired into provider adapters or persistence.
It records only tokens present in a synthetic/raw response; it cannot recover
precision from an already parsed float.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class JsonTokenError(ValueError):
    """The response cannot safely provide an exact-token manifest."""


class _Number(str):
    pass


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonTokenError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise JsonTokenError(f"non-finite JSON token {value!r}")


def _path(parent: str, part: str) -> str:
    escaped = part.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


@dataclass(frozen=True)
class NumericTokenManifest:
    raw_content_hash: str
    tokens_by_path: dict[str, str]


def preserve_numeric_tokens(
    raw: bytes, *, max_bytes: int = 1_000_000, max_depth: int = 100, max_tokens: int = 10_000
) -> NumericTokenManifest:
    """Return exact JSON number lexemes keyed by RFC-6901 paths, or fail closed."""
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise JsonTokenError("raw JSON exceeds exact-token input limit")
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            parse_int=_Number,
            parse_float=_Number,
            parse_constant=_constant,
            object_pairs_hook=_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, JsonTokenError, RecursionError) as exc:
        raise JsonTokenError("invalid exact-token JSON") from exc
    tokens: dict[str, str] = {}

    stack = [(decoded, "", 0)]
    while stack:
        value, path, depth = stack.pop()
        if depth > max_depth:
            raise JsonTokenError("JSON nesting exceeds exact-token limit")
        if isinstance(value, _Number):
            if len(tokens) >= max_tokens:
                raise JsonTokenError("JSON numeric-token count exceeds limit")
            tokens[path] = str(value)
        elif isinstance(value, list):
            stack.extend(
                (child, _path(path, str(index)), depth + 1) for index, child in enumerate(value)
            )
        elif isinstance(value, dict):
            stack.extend((child, _path(path, key), depth + 1) for key, child in value.items())
    return NumericTokenManifest(hashlib.sha256(raw).hexdigest(), tokens)
