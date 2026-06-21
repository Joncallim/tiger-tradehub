from __future__ import annotations

import os
from typing import Any

import httpx

MAX_ERROR_DETAIL_LENGTH = 300


class TradeHubClientError(RuntimeError):
    def __init__(self, method: str, path: str, status_code: int, detail: str):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{method} {path} failed ({status_code}): {detail}")


class TradeHubClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = (
            base_url or os.getenv("TRADEHUB_BASE_URL") or "http://127.0.0.1:8787"
        ).rstrip("/")
        self.api_token = api_token or os.getenv("TRADEHUB_API_TOKEN")
        if not self.api_token:
            raise RuntimeError("TRADEHUB_API_TOKEN is required")
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            response = await client.get(
                f"{self.base_url}{path}", headers=self._headers(), params=params
            )
            return parse_response(response, "GET", path)

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url}{path}", headers=self._headers(), json=payload
            )
            return parse_response(response, "POST", path)


def parse_response(response: httpx.Response, method: str, path: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise TradeHubClientError(
            method, path, response.status_code, response_error_detail(response)
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TradeHubClientError(
            method,
            path,
            response.status_code,
            "TradeHub returned a non-JSON response",
        ) from exc
    if not isinstance(payload, dict):
        raise TradeHubClientError(
            method,
            path,
            response.status_code,
            "TradeHub returned an unexpected response shape",
        )
    return payload


def response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return truncate_detail(response.reason_phrase or "TradeHub request failed")
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str):
            return truncate_detail(message)
    if isinstance(detail, str):
        return truncate_detail(detail)
    return truncate_detail(response.reason_phrase or "TradeHub request failed")


def truncate_detail(value: str) -> str:
    value = " ".join(value.split())
    if len(value) <= MAX_ERROR_DETAIL_LENGTH:
        return value
    return f"{value[:MAX_ERROR_DETAIL_LENGTH]}..."
