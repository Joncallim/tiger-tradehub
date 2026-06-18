# Comparable GitHub Projects

Search date: 2026-06-16

This scan focuses on public GitHub repositories that overlap with Tiger TradeHub's goal: safely
bridging broker APIs into AI assistants, Telegram, or natural-language trading workflows.

## Closest Matches

| Repository | Fit | Notes |
| --- | --- | --- |
| [tigerfintech/openapi-python-sdk](https://github.com/tigerfintech/openapi-python-sdk) | Official Tiger OpenAPI SDK | Upstream SDK used by TradeHub. It includes market data, account, trading, push, CLI, MCP server, and AI skill examples. TradeHub intentionally wraps it with local policy checks and approval gates rather than exposing raw order tools. |
| [tigerfintech/tigeropen-skill](https://github.com/tigerfintech/tigeropen-skill) | Official Tiger AI skill | AI assistant skill package for Tiger OpenAPI usage guidance. It is complementary to TradeHub, but not a full local approval/audit service. |
| [tigerfintech/tigeropen-skill-us](https://github.com/tigerfintech/tigeropen-skill-us) | Official US Tiger AI skill | Similar to `tigeropen-skill`, focused on the US variant. Useful as an upstream reference for assistant-facing Tiger workflows. |
| [clawzhao/tiger_trade_bot](https://github.com/clawzhao/tiger_trade_bot) | Tiger-specific trading bot | Lightweight direct Tiger Open Platform bot, optimized for Raspberry Pi. It overlaps on Tiger order execution, but not on ChatGPT/Claude/Telegram multi-surface access with a shared approval layer. |
| [jpramirez/trade-kit](https://github.com/jpramirez/trade-kit) | Broker CLI toolkit | Open-source CLI toolkit for Tiger Brokers and Moomoo with paper mode and JSON config. Similar local-first broker automation philosophy, but CLI-oriented rather than AI/Telegram gateway-oriented. |
| [goCyberTrade/broker_api_mcp](https://github.com/goCyberTrade/broker_api_mcp) | Multi-broker MCP | MCP interface wrapper for broker account and transaction APIs, including Tiger Brokers. This is the closest MCP concept match; TradeHub is narrower and adds explicit REST/OpenAPI and Telegram paths plus confirmation-token order submission. |
| [arg-foo/tiger-brokers-cash-mcp](https://github.com/arg-foo/tiger-brokers-cash-mcp) | Tiger MCP niche | Tiger Brokers cash/account MCP project. It appears focused on account data rather than a guarded order-execution bridge. |
| [Joash-JW/Auto-DCA](https://github.com/Joash-JW/Auto-DCA) | Tiger order tutorial | Tutorial for periodic DCA orders across brokers, including Tiger. Useful as a simple reference for broker-specific order mechanics. |
| [marketcalls/openalgo](https://github.com/marketcalls/openalgo) | Broad trading platform | OpenAlgo has AI-powered trading via MCP, ChatGPT/Claude compatibility, Telegram integration, broker integrations, and strategy tooling. It is much broader than TradeHub; TradeHub is a compact Tiger-specific gateway. |
| [jackson-video-resources/claude-tradingview-mcp-trading](https://github.com/jackson-video-resources/claude-tradingview-mcp-trading) | AI-to-trading MCP example | Claude/TradingView/BitGet automation project. Relevant for natural-language and MCP-driven trade execution patterns, but focused on TradingView and crypto exchange execution. |
| [bidouilles/mcp-tradingview-server](https://github.com/bidouilles/mcp-tradingview-server) | Market-data MCP | TradingView indicator and market-data MCP server. It overlaps on analysis tooling, not broker order placement. |
| [jinxiy1104/MockTrader_MCP](https://github.com/jinxiy1104/MockTrader_MCP) | Risk and sandbox model | MCP trading sandbox with risk rules and historical replay. Relevant to future TradeHub testing and simulation ideas. |

## Differentiation

TradeHub is intentionally narrower than the broad trading platforms and safer than a raw broker MCP
toolbox:

- Tiger-specific integration using Tiger's official Python SDK.
- One policy engine shared by REST, MCP, and Telegram.
- Preview-first order flow with short-lived confirmation tokens.
- Dry-run mode on by default.
- Local SQLite audit log.
- Claude-first compatibility through a thin MCP adapter that calls the guarded local API.
- ChatGPT Actions compatibility through FastAPI's OpenAPI schema for advanced per-user deployments.
- Telegram commands that use the same preview/confirm path rather than placing orders directly.

## Follow-Up Ideas

- Add a broker abstraction only if a second broker is needed; current scope should stay Tiger-specific.
- Add a paper-account smoke test fixture once Tiger developer credentials are available.
- Add configurable approval channels, for example requiring Telegram confirmation for ChatGPT-originated
  order previews.
- Add portfolio and buying-power endpoints before enabling live trading.
- Add replay tests inspired by `MockTrader_MCP` for policy regression coverage.
