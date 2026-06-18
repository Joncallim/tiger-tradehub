from __future__ import annotations

from functools import lru_cache

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from tradehub.audit import AuditStore
from tradehub.config import Settings, get_settings
from tradehub.models import (
    AccountAssetsResponse,
    CancelOrderRequest,
    HealthResponse,
    OrderIntent,
    OrdersResponse,
    PositionsResponse,
    PreviewResponse,
    SubmitOrderRequest,
    SubmitOrderResponse,
)
from tradehub.policy import PolicyError, validate_order_intent
from tradehub.tiger_gateway import TigerGateway

app = FastAPI(
    title="Tiger TradeHub",
    version="0.1.0",
    description="Guarded Tiger Brokers trading bridge for ChatGPT, Claude, and Telegram.",
)


@lru_cache
def get_store() -> AuditStore:
    return AuditStore(get_settings().database_path)


@lru_cache
def get_gateway() -> TigerGateway:
    return TigerGateway(get_settings())


def require_auth(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = f"Bearer {settings.api_token}"
    if not authorization or authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid bearer token",
        )


@app.get("/health", response_model=HealthResponse, dependencies=[Depends(require_auth)])
def health(
    settings: Settings = Depends(get_settings),
    gateway: TigerGateway = Depends(get_gateway),
):
    return HealthResponse(
        ok=True,
        dry_run=settings.dry_run,
        tiger_configured=gateway.is_configured(),
        require_approval=settings.require_approval,
    )


@app.post("/orders/preview", response_model=PreviewResponse, dependencies=[Depends(require_auth)])
def preview_order(
    intent: OrderIntent,
    settings: Settings = Depends(get_settings),
    store: AuditStore = Depends(get_store),
    gateway: TigerGateway = Depends(get_gateway),
):
    try:
        warnings = validate_order_intent(intent, settings)
    except PolicyError as exc:
        store.record_event("policy_block", {"intent": intent.model_dump(), "reason": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    tiger_preview = gateway.preview_order(intent)
    token = None
    expires_at = None
    if settings.require_approval:
        token, expires_at = store.create_confirmation(
            intent, tiger_preview, settings.confirmation_ttl_seconds
        )

    return PreviewResponse(
        accepted=True,
        dry_run=settings.dry_run,
        intent=intent,
        confirmation_token=token,
        expires_at=expires_at,
        policy_warnings=warnings,
        tiger_preview=tiger_preview,
    )


@app.post(
    "/orders/submit",
    response_model=SubmitOrderResponse,
    dependencies=[Depends(require_auth)],
)
def submit_order(
    request: SubmitOrderRequest,
    settings: Settings = Depends(get_settings),
    store: AuditStore = Depends(get_store),
    gateway: TigerGateway = Depends(get_gateway),
):
    try:
        intent, tiger_preview = store.consume_confirmation(request.confirmation_token)
        validate_order_intent(intent, settings)
    except (KeyError, ValueError, PolicyError) as exc:
        store.record_event("submit_block", {"reason": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if settings.dry_run:
        store.record_event("dry_run_submit", {"intent": intent.model_dump()})
        return SubmitOrderResponse(
            submitted=False,
            dry_run=True,
            intent=intent,
            tiger_response=tiger_preview,
        )

    try:
        order_id, tiger_response = gateway.place_order(intent)
    except Exception as exc:
        store.record_event("submit_error", {"intent": intent.model_dump(), "reason": str(exc)})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    store.mark_order_id(request.confirmation_token, order_id)
    store.record_event("live_submit", {"intent": intent.model_dump(), "order_id": order_id})
    return SubmitOrderResponse(
        submitted=True,
        dry_run=False,
        order_id=order_id,
        intent=intent,
        tiger_response=tiger_response,
    )


@app.post("/orders/cancel", dependencies=[Depends(require_auth)])
def cancel_order(
    request: CancelOrderRequest,
    settings: Settings = Depends(get_settings),
    store: AuditStore = Depends(get_store),
    gateway: TigerGateway = Depends(get_gateway),
):
    if settings.dry_run:
        store.record_event("dry_run_cancel", {"order_id": request.order_id})
        return {"cancelled": False, "dry_run": True, "order_id": request.order_id}
    try:
        response = gateway.cancel_order(request.order_id)
    except Exception as exc:
        store.record_event("cancel_error", {"order_id": request.order_id, "reason": str(exc)})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    store.record_event("cancel", {"order_id": request.order_id})
    return {
        "cancelled": True,
        "dry_run": False,
        "order_id": request.order_id,
        "tiger_response": response,
    }


@app.get(
    "/account/assets",
    response_model=AccountAssetsResponse,
    dependencies=[Depends(require_auth)],
)
def account_assets(
    store: AuditStore = Depends(get_store),
    gateway: TigerGateway = Depends(get_gateway),
):
    if not gateway.is_configured():
        return AccountAssetsResponse(
            tiger_configured=False,
            warning="Tiger credentials are not configured",
        )
    try:
        assets = gateway.get_assets()
    except Exception as exc:
        store.record_event("read_error", {"resource": "account_assets", "reason": str(exc)})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    store.record_event("read_account_assets", {})
    return AccountAssetsResponse(tiger_configured=True, assets=assets)


@app.get(
    "/account/positions",
    response_model=PositionsResponse,
    dependencies=[Depends(require_auth)],
)
def account_positions(
    symbol: str | None = Query(default=None, min_length=1, max_length=16),
    store: AuditStore = Depends(get_store),
    gateway: TigerGateway = Depends(get_gateway),
):
    if not gateway.is_configured():
        return PositionsResponse(
            tiger_configured=False,
            warning="Tiger credentials are not configured",
        )
    try:
        positions = gateway.get_positions(symbol=symbol)
    except Exception as exc:
        store.record_event(
            "read_error",
            {"resource": "account_positions", "symbol": symbol, "reason": str(exc)},
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    store.record_event("read_account_positions", {"symbol": symbol})
    return PositionsResponse(tiger_configured=True, positions=positions)


@app.get(
    "/account/orders",
    response_model=OrdersResponse,
    dependencies=[Depends(require_auth)],
)
def account_orders(
    symbol: str | None = Query(default=None, min_length=1, max_length=16),
    limit: int = Query(default=20, ge=1, le=100),
    store: AuditStore = Depends(get_store),
    gateway: TigerGateway = Depends(get_gateway),
):
    if not gateway.is_configured():
        return OrdersResponse(
            tiger_configured=False,
            warning="Tiger credentials are not configured",
        )
    try:
        orders = gateway.get_orders(symbol=symbol, limit=limit)
    except Exception as exc:
        store.record_event(
            "read_error",
            {
                "resource": "account_orders",
                "symbol": symbol,
                "limit": limit,
                "reason": str(exc),
            },
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    store.record_event("read_account_orders", {"symbol": symbol, "limit": limit})
    return OrdersResponse(tiger_configured=True, orders=orders)


def main() -> None:
    settings = get_settings()
    uvicorn.run("tradehub.app:app", host=settings.bind_host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
