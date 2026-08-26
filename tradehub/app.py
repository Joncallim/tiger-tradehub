from __future__ import annotations

import hmac
import logging
import re
import uuid
from functools import lru_cache
from time import sleep
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from tradehub.audit import AuditStore
from tradehub.config import Settings, get_settings, secret_value
from tradehub.models import (
    AccountAssetsResponse,
    CancelOrderRequest,
    CancelOrderResponse,
    HealthResponse,
    OrderIntent,
    OrdersResponse,
    PositionsResponse,
    PreviewResponse,
    ReconcileOrderRequest,
    ReconcileOrderResponse,
    ResolveOrderRequest,
    ResolveOrderResponse,
    SubmitOrderRequest,
    SubmitOrderResponse,
)
from tradehub.policy import PolicyError, validate_order_intent
from tradehub.tiger_gateway import TigerGateway, classify_preview

logger = logging.getLogger(__name__)
UPSTREAM_ERROR_MESSAGE = "upstream broker request failed"
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
RECONCILE_ORDER_ID_RETRY_DELAY_SECONDS = 0.05

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
    token = None
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
    expected = settings.api_token.get_secret_value()
    if token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid bearer token",
        )


def require_preview_auth(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    token = None
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
    if settings.preview_api_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="preview capability is not configured",
        )
    if token is None or not hmac.compare_digest(token, settings.preview_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid preview capability",
        )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    error_id = uuid.uuid4().hex
    logger.exception("Unhandled API error %s on %s %s", error_id, request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": {"message": "internal server error", "error_id": error_id}},
    )


def redact_sensitive(text: str, settings: Settings) -> str:
    redacted = PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", text)
    sensitive_values = [
        settings.api_token.get_secret_value(),
        settings.tiger_id,
        settings.tiger_account,
        secret_value(settings.tiger_private_key),
        secret_value(settings.telegram_bot_token),
    ]
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def upstream_error_detail() -> dict[str, str]:
    return {"message": UPSTREAM_ERROR_MESSAGE, "error_id": uuid.uuid4().hex}


def record_upstream_error(
    store: AuditStore,
    settings: Settings,
    event_type: str,
    payload: dict[str, Any],
    exc: Exception,
    detail: dict[str, str],
) -> None:
    logger.exception("%s: %s", detail["error_id"], UPSTREAM_ERROR_MESSAGE)
    store.record_event(
        event_type,
        {
            **payload,
            "reason": redact_sensitive(str(exc), settings),
            "error_id": detail["error_id"],
        },
    )


def get_order_with_retries(gateway: TigerGateway, order_id: str) -> dict[str, Any] | None:
    order = gateway.get_order(order_id)
    if order is not None:
        return order

    sleep(RECONCILE_ORDER_ID_RETRY_DELAY_SECONDS)
    order = gateway.get_order(order_id)
    if order is not None:
        return order

    for entry in gateway.get_orders(limit=100):
        if str(entry.get("order_id")) == str(order_id) or str(entry.get("id")) == str(order_id):
            return entry

    return None


@app.get("/health", response_model=HealthResponse, dependencies=[Depends(require_auth)])
def health(
    settings: Settings = Depends(get_settings),
    gateway: TigerGateway = Depends(get_gateway),
):
    return HealthResponse(
        ok=True,
        dry_run=settings.dry_run,
        tiger_configured=gateway.is_configured(),
        require_approval=True,
    )


@app.post(
    "/orders/preview",
    response_model=PreviewResponse,
    dependencies=[Depends(require_preview_auth)],
)
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    try:
        tiger_preview = gateway.preview_order(intent)
    except Exception as exc:
        detail = upstream_error_detail()
        record_upstream_error(
            store,
            settings,
            "preview_error",
            {"intent": intent.model_dump()},
            exc,
            detail,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

    preview_status = classify_preview(tiger_preview)
    if preview_status != "accepted":
        store.record_event(
            "preview_rejected" if preview_status == "rejected" else "preview_ambiguous",
            {"intent": intent.model_dump(), "tiger_preview": tiger_preview},
        )
        return PreviewResponse(
            accepted=False,
            dry_run=settings.dry_run,
            intent=intent,
            confirmation_token=None,
            expires_at=None,
            policy_warnings=warnings,
            tiger_preview=tiger_preview,
        )

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
        intent, tiger_preview, submit_lease_id = store.claim_confirmation(
            request.confirmation_token
        )
    except (KeyError, ValueError) as exc:
        store.record_event("submit_block", {"reason": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    try:
        validate_order_intent(intent, settings)
    except PolicyError as exc:
        try:
            store.release_confirmation(request.confirmation_token, submit_lease_id)
        except ValueError as lease_exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(lease_exc)
            ) from lease_exc
        store.record_event("submit_block", {"reason": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if settings.dry_run:
        try:
            store.finalize_confirmation(request.confirmation_token, submit_lease_id=submit_lease_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        store.record_event("dry_run_submit", {"intent": intent.model_dump()})
        return SubmitOrderResponse(
            submitted=False,
            dry_run=True,
            intent=intent,
            tiger_response=tiger_preview,
        )

    reserved_order_id: str | None = None
    order_created = False
    try:
        _, _, existing_reserved_order_id = store.get_confirmation(request.confirmation_token)
        reserved_order_id = existing_reserved_order_id
        order = gateway.create_order(intent)
        order_created = True
        if existing_reserved_order_id is not None:
            gateway.assign_order_id(order, existing_reserved_order_id)
        reserved_order_id = gateway.get_order_id(order)
        store.record_reserved_order_id(
            request.confirmation_token, reserved_order_id, submit_lease_id
        )
        store.mark_submission_in_progress(
            request.confirmation_token, reserved_order_id, submit_lease_id
        )
        order_id, tiger_response = gateway.place_order(order)
    except Exception as exc:
        detail = upstream_error_detail()
        payload = {"intent": intent.model_dump(), "reserved_order_id": reserved_order_id}
        if order_created:
            record_upstream_error(
                store,
                settings,
                "submit_indeterminate",
                payload,
                exc,
                detail,
            )
            try:
                store.mark_submission_indeterminate(
                    request.confirmation_token,
                    submit_lease_id,
                    reserved_order_id,
                )
            except ValueError as lease_exc:
                store.record_event("submit_block", {"reason": str(lease_exc)})
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(lease_exc)
                ) from lease_exc
        else:
            record_upstream_error(
                store,
                settings,
                "submit_error",
                payload,
                exc,
                detail,
            )
            try:
                store.release_confirmation(request.confirmation_token, submit_lease_id)
            except ValueError as lease_exc:
                store.record_event("submit_block", {"reason": str(lease_exc)})
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(lease_exc)
                ) from lease_exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

    try:
        store.finalize_confirmation(
            request.confirmation_token, order_id, submit_lease_id=submit_lease_id
        )
    except ValueError as exc:
        store.record_event("submit_block", {"reason": str(exc)})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    store.record_event("live_submit", {"intent": intent.model_dump(), "order_id": order_id})
    return SubmitOrderResponse(
        submitted=True,
        dry_run=False,
        order_id=order_id,
        intent=intent,
        tiger_response=tiger_response,
    )


@app.post(
    "/orders/submit/reconcile",
    response_model=ReconcileOrderResponse,
    dependencies=[Depends(require_auth)],
)
def reconcile_order(
    request: ReconcileOrderRequest,
    settings: Settings = Depends(get_settings),
    store: AuditStore = Depends(get_store),
    gateway: TigerGateway = Depends(get_gateway),
):
    try:
        intent, tiger_preview, reserved_order_id, reconcile_lease_id, prior_state = (
            store.claim_reconciliation_confirmation(request.confirmation_token)
        )
    except (KeyError, ValueError) as exc:
        store.record_event("reconcile_block", {"reason": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if not reserved_order_id:
        try:
            store.abandon_reconciliation(request.confirmation_token, reconcile_lease_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        store.record_event(
            "manual_reconciliation_required",
            {
                "confirmation_token": request.confirmation_token,
                "reason": "missing reserved_order_id",
            },
        )
        return ReconcileOrderResponse(
            status="manual_reconciliation_required",
            submitted=False,
            intent=intent,
            tiger_response=tiger_preview,
        )

    try:
        order = get_order_with_retries(gateway, reserved_order_id)
    except Exception as exc:
        detail = upstream_error_detail()
        record_upstream_error(
            store,
            settings,
            "reconcile_error",
            {
                "confirmation_token": request.confirmation_token,
                "reserved_order_id": reserved_order_id,
            },
            exc,
            detail,
        )
        try:
            store.abandon_reconciliation(request.confirmation_token, reconcile_lease_id)
        except ValueError as lease_exc:
            store.record_event("reconcile_block", {"reason": str(lease_exc)})
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(lease_exc)
            ) from lease_exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

    if order is not None:
        resolved_order_id = gateway.get_global_order_id(order)
        if resolved_order_id is None:
            try:
                store.abandon_reconciliation(request.confirmation_token, reconcile_lease_id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"message": "upstream broker response missing global order id"},
            )
        try:
            store.finalize_confirmation(
                request.confirmation_token,
                resolved_order_id,
                reconcile_lease_id=reconcile_lease_id,
            )
        except ValueError as exc:
            store.record_event("reconcile_block", {"reason": str(exc)})
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        store.record_event(
            "submit_reconciled",
            {"intent": intent.model_dump(), "order_id": resolved_order_id},
        )
        return ReconcileOrderResponse(
            status="resolved",
            submitted=True,
            intent=intent,
            order_id=resolved_order_id,
            tiger_response=order,
        )

    # A stolen SUBMITTING worker may still be inside place_order. Broker absence at
    # this instant cannot fence that call, so preserve a non-retryable indeterminate
    # state; only ordinary indeterminate failures become retryable.
    if prior_state == "SUBMITTING":
        try:
            store.preserve_stolen_submission(request.confirmation_token, reconcile_lease_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        store.record_event(
            "submit_reconcile_pending",
            {"confirmation_token": request.confirmation_token, "intent": intent.model_dump()},
        )
        return ReconcileOrderResponse(
            status="indeterminate", submitted=False, intent=intent, tiger_response=tiger_preview
        )

    try:
        store.mark_reconciliation_retry_allowed(request.confirmation_token, reconcile_lease_id)
    except ValueError as exc:
        store.record_event("reconcile_block", {"reason": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    store.record_event(
        "submit_reconcile_retryable",
        {"confirmation_token": request.confirmation_token, "intent": intent.model_dump()},
    )
    return ReconcileOrderResponse(
        status="retryable",
        submitted=False,
        intent=intent,
        tiger_response=tiger_preview,
    )


@app.post(
    "/orders/submit/resolve",
    response_model=ResolveOrderResponse,
    dependencies=[Depends(require_auth)],
)
def resolve_order(
    request: ResolveOrderRequest,
    store: AuditStore = Depends(get_store),
):
    try:
        resolved_state = store.resolve_indeterminate_confirmation(
            request.confirmation_token,
            request.resolver,
            request.global_order_id,
            request.no_submission_occurred,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return ResolveOrderResponse(
        status="submitted" if resolved_state == "SUBMITTED" else "retryable",
        submitted=resolved_state == "SUBMITTED",
        order_id=request.global_order_id,
    )


@app.post(
    "/orders/cancel",
    response_model=CancelOrderResponse,
    dependencies=[Depends(require_auth)],
)
def cancel_order(
    request: CancelOrderRequest,
    settings: Settings = Depends(get_settings),
    store: AuditStore = Depends(get_store),
    gateway: TigerGateway = Depends(get_gateway),
):
    if settings.dry_run:
        store.record_event("dry_run_cancel", {"order_id": request.order_id})
        return CancelOrderResponse(cancelled=False, dry_run=True, order_id=request.order_id)
    try:
        response = gateway.cancel_order(request.order_id)
    except Exception as exc:
        detail = upstream_error_detail()
        record_upstream_error(
            store,
            settings,
            "cancel_error",
            {"order_id": request.order_id},
            exc,
            detail,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
    store.record_event("cancel", {"order_id": request.order_id})
    return CancelOrderResponse(
        cancelled=True,
        dry_run=False,
        order_id=request.order_id,
        tiger_response=response,
    )


@app.get(
    "/account/assets",
    response_model=AccountAssetsResponse,
    dependencies=[Depends(require_auth)],
)
def account_assets(
    settings: Settings = Depends(get_settings),
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
        detail = upstream_error_detail()
        record_upstream_error(
            store, settings, "read_error", {"resource": "account_assets"}, exc, detail
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
    store.record_event("read_account_assets", {})
    return AccountAssetsResponse(tiger_configured=True, assets=assets)


@app.get(
    "/account/positions",
    response_model=PositionsResponse,
    dependencies=[Depends(require_auth)],
)
def account_positions(
    symbol: str | None = Query(default=None, min_length=1, max_length=16),
    settings: Settings = Depends(get_settings),
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
        detail = upstream_error_detail()
        record_upstream_error(
            store,
            settings,
            "read_error",
            {"resource": "account_positions", "symbol": symbol},
            exc,
            detail,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
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
    settings: Settings = Depends(get_settings),
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
        detail = upstream_error_detail()
        record_upstream_error(
            store,
            settings,
            "read_error",
            {"resource": "account_orders", "symbol": symbol, "limit": limit},
            exc,
            detail,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
    store.record_event("read_account_orders", {"symbol": symbol, "limit": limit})
    return OrdersResponse(tiger_configured=True, orders=orders)


def main() -> None:
    settings = get_settings()
    uvicorn.run("tradehub.app:app", host=settings.bind_host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
