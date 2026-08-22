# Tiger OpenAPI Rate Limits

Source: https://docs-en.itigerup.com/docs/ratelimit.md (updated 2026-08-20).

Limits are applied per **tigerId + wire method**, 60-second rolling window.
Order placement/modification additionally share an account-level limit.

| Tier             | Limit    | Counts | Methods (wire names) |
| ---------------- | -------- | ------ | -------------------- |
| High-frequency   | 120/min  | 21     | `order_no`, `place_order`, `modify_order`, `cancel_order`, `orders`, `active_orders`, `inactive_orders`, `filled_orders`, `timeline`, `hour_trading_timeline`, `trade_tick`, `quote_real_time`, `quote_overnight`, `option_trade_tick`, `option_brief`, futures quote methods |
| Medium-frequency | 60/min   | 63     | `batch_place_order`, `order_executions`, `order_transactions`, `accounts`, `assets`, `prime_assets`, `aggregate_assets`, `positions`, `partition_account`, `user_transactions`, `user_login`, `user_trade_token`, `contract`, `contracts`, `brief`, `quote_depth`, `kline`, `history_timeline`, option chain methods, `fund_transfer`, `withdrawals`, option exercise, more |
| Low-frequency    | 10/min   | 16     | `grab_quote_permission`, `get_quote_permission`, `market_state`, `quote_delay`, `user_license`, `user_token_refresh`, `kline_quota`, `all_symbol_names`, `all_symbols`, `stock_detail`, `market_scanner`, `market_scanner_tags`, `trade_rank`, `broker_hold`, `future_exchange`, `fund_details` |

## Shared order limit

`place_order` and `modify_order` share **5 requests/second per account** (both methods
counted together). A request must satisfy BOTH the method-level limit AND this shared limit.

## Error & enforcement

- Limit hit: HTTP 429, code `5`, `msg=rate limit error(current limiting interface:<name>, up to N times per minute)`.
- **Repeatedly exceeding limits can auto-blacklist the account** (no further API requests).
- Quotas can be upgraded (for a fee) at https://developer.itigerup.com/profile.

## TradeHub-relevant notes

- A strategy loop should budget per method: e.g. `quote_real_time` 120/min, `kline` 60/min,
  `assets`/`positions` 60/min, `market_state` 10/min (polling market status is the tightest).
- Batch endpoints (`batch_place_order`) count once per call against their own 60/min bucket —
  prefer batching over N single `place_order` calls, which also hit the 5 req/s account cap.
- Respect 429s with backoff; circuit-break on repeated 429s to avoid blacklist.