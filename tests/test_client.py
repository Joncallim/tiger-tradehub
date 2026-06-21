import asyncio

import httpx
import pytest

from tradehub.client import TradeHubClient, TradeHubClientError


def test_client_errors_are_bounded_and_do_not_include_token_or_base_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sensitive-token"
        return httpx.Response(
            422,
            json={"detail": {"message": "invalid order"}},
            request=request,
        )

    client = TradeHubClient(
        base_url="http://tradehub.example",
        api_token="sensitive-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TradeHubClientError) as exc_info:
        asyncio.run(client.get("/health"))

    message = str(exc_info.value)
    assert message == "GET /health failed (422): invalid order"
    assert "sensitive-token" not in message
    assert "tradehub.example" not in message


def test_client_rejects_unexpected_success_response_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"ok": True}], request=request)

    client = TradeHubClient(
        base_url="http://127.0.0.1:8787",
        api_token="sensitive-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TradeHubClientError, match="unexpected response shape"):
        asyncio.run(client.get("/health"))
