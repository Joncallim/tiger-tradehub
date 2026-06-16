from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderIntent(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    symbol: str = Field(min_length=1, max_length=16, examples=["AAPL"])
    side: Side
    quantity: float = Field(gt=0, examples=[1])
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = Field(default=None, gt=0, examples=[150.0])
    currency: str = Field(default="USD", min_length=3, max_length=3)
    reason: str | None = Field(default=None, max_length=500)
    client_request_id: str | None = Field(default=None, max_length=128)

    @field_validator("symbol", "currency")
    @classmethod
    def uppercase(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_price(self) -> OrderIntent:
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")
        if self.order_type == OrderType.MARKET and self.limit_price is not None:
            raise ValueError("limit_price must be omitted for MARKET orders")
        return self

    @property
    def estimated_notional(self) -> float | None:
        if self.limit_price is None:
            return None
        return self.quantity * self.limit_price


class PreviewResponse(BaseModel):
    accepted: bool
    dry_run: bool
    intent: OrderIntent
    confirmation_token: str | None
    expires_at: datetime | None
    policy_warnings: list[str] = Field(default_factory=list)
    tiger_preview: dict[str, Any] | None = None


class SubmitOrderRequest(BaseModel):
    confirmation_token: str = Field(min_length=16)


class SubmitOrderResponse(BaseModel):
    submitted: bool
    dry_run: bool
    order_id: str | None = None
    intent: OrderIntent
    tiger_response: dict[str, Any] | None = None


class CancelOrderRequest(BaseModel):
    order_id: str = Field(min_length=1)


class HealthResponse(BaseModel):
    ok: bool
    dry_run: bool
    tiger_configured: bool
    require_approval: bool

