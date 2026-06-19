from __future__ import annotations

from tradehub.config import Settings
from tradehub.models import OrderIntent, OrderType


class PolicyError(ValueError):
    pass


def validate_order_intent(intent: OrderIntent, settings: Settings) -> list[str]:
    warnings: list[str] = []

    if settings.symbol_allowlist and intent.symbol not in settings.symbol_allowlist:
        allowed = ", ".join(sorted(settings.symbol_allowlist))
        raise PolicyError(f"{intent.symbol} is not in TRADEHUB_SYMBOL_ALLOWLIST: {allowed}")

    if intent.quantity > settings.max_quantity:
        raise PolicyError(
            f"quantity {intent.quantity:g} exceeds TRADEHUB_MAX_QUANTITY {settings.max_quantity:g}"
        )

    if intent.currency != "USD":
        raise PolicyError("only USD-denominated orders are supported")

    if intent.order_type == OrderType.MARKET:
        raise PolicyError("market orders are not supported")

    notional = intent.estimated_notional
    if notional is not None and notional > settings.max_notional_usd:
        raise PolicyError(
            f"estimated notional {notional:.2f} exceeds TRADEHUB_MAX_NOTIONAL_USD "
            f"{settings.max_notional_usd:.2f}"
        )

    if settings.dry_run:
        warnings.append("Dry-run mode is active; submit will not place a live Tiger order.")

    return warnings
