from __future__ import annotations

import os
from typing import Any

import httpx


class TradeHubClient:
    def __init__(self, base_url: str | None = None, api_token: str | None = None):
        self.base_url = (base_url or os.getenv("TRADEHUB_BASE_URL") or "http://127.0.0.1:8787").rstrip(
            "/"
        )
        self.api_token = api_token or os.getenv("TRADEHUB_API_TOKEN")
        if not self.api_token:
            raise RuntimeError("TRADEHUB_API_TOKEN is required")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    async def get(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}{path}", headers=self._headers(), json=payload
            )
            response.raise_for_status()
            return response.json()

