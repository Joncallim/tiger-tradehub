from __future__ import annotations

from typing import Any

from tradehub.config import Settings
from tradehub.models import OrderIntent, OrderType


class TigerGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._trade_client: Any | None = None

    def is_configured(self) -> bool:
        return self.settings.tiger_configured

    def preview_order(self, intent: OrderIntent) -> dict[str, Any] | None:
        if not self.is_configured():
            return {"status": "skipped", "reason": "Tiger credentials are not configured"}
        order = self._build_order(intent)
        response = self.trade_client.preview_order(order)
        return normalize(response)

    def place_order(self, intent: OrderIntent) -> tuple[str | None, dict[str, Any] | None]:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        order = self._build_order(intent)
        response = self.trade_client.place_order(order)
        order_id = getattr(order, "id", None) or response
        return str(order_id) if order_id is not None else None, normalize(response or order)

    def cancel_order(self, order_id: str) -> dict[str, Any] | None:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        response = self.trade_client.cancel_order(order_id)
        return normalize(response)

    @property
    def trade_client(self) -> Any:
        if self._trade_client is None:
            self._trade_client = self._create_trade_client()
        return self._trade_client

    def _create_trade_client(self) -> Any:
        from tigeropen.common.consts import Language
        from tigeropen.common.util.signature_utils import read_private_key
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        from tigeropen.trade.trade_client import TradeClient

        config = TigerOpenClientConfig(sandbox_debug=self.settings.tiger_sandbox)
        config.tiger_id = self.settings.tiger_id
        config.account = self.settings.tiger_account
        config.language = Language.en_US

        if self.settings.tiger_private_key:
            config.private_key = self.settings.tiger_private_key.replace("\\n", "\n")
        elif self.settings.tiger_private_key_path:
            config.private_key = read_private_key(str(self.settings.tiger_private_key_path))

        return TradeClient(config)

    def _build_order(self, intent: OrderIntent) -> Any:
        from tigeropen.common.util.contract_utils import stock_contract
        from tigeropen.common.util.order_utils import limit_order, market_order

        contract = stock_contract(symbol=intent.symbol, currency=intent.currency)
        if intent.order_type == OrderType.LIMIT:
            return limit_order(
                account=self.settings.tiger_account,
                contract=contract,
                action=intent.side.value,
                quantity=intent.quantity,
                limit_price=intent.limit_price,
            )
        return market_order(
            account=self.settings.tiger_account,
            contract=contract,
            action=intent.side.value,
            quantity=intent.quantity,
        )


def normalize(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_") and isinstance(item, str | int | float | bool | type(None))
        }
    return {"value": str(value)}

