from __future__ import annotations

from typing import Any

from tradehub.config import Settings, secret_value
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
        order_id = extract_order_id(response) or extract_order_id(order)
        return str(order_id) if order_id is not None else None, normalize(response or order)

    def cancel_order(self, order_id: str) -> dict[str, Any] | None:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        response = self.trade_client.cancel_order(
            account=self.settings.tiger_account, order_id=order_id
        )
        return normalize(response)

    def get_assets(self) -> dict[str, Any] | None:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        response = self.trade_client.get_assets(account=self.settings.tiger_account)
        return normalize(response)

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        response = self.trade_client.get_positions(
            account=self.settings.tiger_account,
            symbol=symbol.upper() if symbol else None,
        )
        return normalize_collection(response)

    def get_orders(self, symbol: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        response = self.trade_client.get_orders(
            account=self.settings.tiger_account,
            symbol=symbol.upper() if symbol else None,
            limit=limit,
            is_brief=True,
        )
        return normalize_collection(response)

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
        if self.settings.tiger_license:
            config.license = self.settings.tiger_license
        config.language = Language.en_US

        private_key = secret_value(self.settings.tiger_private_key)
        if private_key:
            config.private_key = private_key.replace("\\n", "\n")
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
        return {key: _json_safe(item) for key, item in value.items()}
    if hasattr(value, "to_dict"):
        return {key: _json_safe(item) for key, item in value.to_dict().items()}
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_") and isinstance(item, str | int | float | bool | type(None))
        }
    return {"value": str(value)}


def _json_safe(value: Any) -> Any:
    """Recursively convert SDK objects (e.g. tigeropen Contract) into JSON-safe values."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    if hasattr(value, "__dict__"):
        return {
            key: _json_safe(item) for key, item in vars(value).items() if not key.startswith("_")
        }
    return str(value)


def extract_order_id(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str | int):
        return value
    if isinstance(value, dict):
        return value.get("order_id") or value.get("id")
    return getattr(value, "order_id", None) or getattr(value, "id", None)


def normalize_collection(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [item for item in (normalize(entry) for entry in value) if item is not None]
    item = normalize(value)
    return [item] if item is not None else []
