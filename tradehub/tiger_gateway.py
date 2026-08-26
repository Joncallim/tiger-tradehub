from __future__ import annotations

from typing import Any

from tradehub.config import Settings, secret_value
from tradehub.models import OrderIntent, OrderType

PREVIEW_SUCCESS_FIELDS = frozenset(
    {
        "init_margin",
        "maint_margin",
        "equity_with_loan",
        "margin_currency",
        "commission_currency",
        "commission",
    }
)
DOCUMENTED_PREVIEW_SUCCESS_FIELDS = frozenset(
    {
        "init_margin_before",
        "init_margin",
        "maint_margin_before",
        "maint_margin",
        "margin_currency",
        "equity_with_loan_before",
        "equity_with_loan",
        "min_commission",
        "max_commission",
        "commission_currency",
    }
)


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

    def create_order(self, intent: OrderIntent) -> Any:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        order = self._build_order(intent)
        return self.trade_client.create_order(
            account=self.settings.tiger_account,
            contract=order.contract,
            action=order.action,
            order_type=order.order_type,
            quantity=order.quantity,
            limit_price=order.limit_price,
        )

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        getter = getattr(self.trade_client, "get_order", None)
        if callable(getter):
            # create_order.order_id is the account-specific reserved number. Tiger's
            # `id` parameter is a different namespace: the global order identifier.
            response = getter(
                account=self.settings.tiger_account,
                order_id=int(order_id) if str(order_id).isdigit() else order_id,
            )
            response_value = normalize(response)
            if response_value:
                return response_value

        return next(
            (
                entry
                for entry in self.get_orders(limit=100)
                if str(entry.get("order_id")) == str(order_id)
                or str(entry.get("id")) == str(order_id)
            ),
            None,
        )

    def place_order(self, order: OrderIntent | Any) -> tuple[str | None, dict[str, Any] | None]:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        order_to_send = self._build_order(order) if isinstance(order, OrderIntent) else order
        response = self.trade_client.place_order(order_to_send)
        order_id = extract_order_id(response) or extract_order_id(order_to_send)
        return str(order_id) if order_id is not None else None, normalize(response or order_to_send)

    def assign_order_id(self, order: Any, order_id: str) -> Any:
        order.order_id = order_id
        return order

    def get_order_id(self, order: Any) -> str | None:
        value = extract_order_id(order)
        return str(value) if value is not None else None

    def get_global_order_id(self, order: Any) -> str | None:
        normalized = normalize(order)
        value = normalized.get("id") if normalized else None
        return str(value) if value is not None else None

    def cancel_order(self, order_id: str) -> dict[str, Any] | None:
        if not self.is_configured():
            raise RuntimeError("Tiger credentials are not configured")
        # tigeropen's cancel_order takes two distinct params: `id` (global order id,
        # what place_order returns and what we record) and `order_id` (account-specific
        # id). Passing the global id into `order_id` is rejected by Tiger's API.
        response = self.trade_client.cancel_order(
            account=self.settings.tiger_account, id=int(order_id)
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


def classify_preview(value: dict[str, Any] | None) -> str:
    """Classify only the installed SDK's documented preview shapes."""
    if not isinstance(value, dict):
        return "unknown"
    warning = value.get("warning_text")
    message = value.get("message")
    if (isinstance(warning, str) and warning.strip()) or (
        isinstance(message, str) and message.strip()
    ):
        return "rejected"
    if value.get("is_pass") is False:
        return "rejected"
    if value.get("is_pass") is True and PREVIEW_SUCCESS_FIELDS.issubset(value):
        return "accepted"
    if DOCUMENTED_PREVIEW_SUCCESS_FIELDS.issubset(value):
        return "accepted"
    return "unknown"
