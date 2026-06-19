# Security Policy

## Scope

Tiger TradeHub is a **local-only** bridge between AI clients (Claude, Telegram) and Tiger Brokers
OpenAPI. It is not a hosted service. The attack surface is intentionally narrow: the REST API binds
to `127.0.0.1` by default and is reachable only from the same machine.

Vulnerabilities in scope:

- Authentication bypass (bearer token handling in `tradehub/app.py`)
- Confirmation token replay or TTL bypass (`tradehub/audit.py`)
- Policy engine bypass (symbol allowlist, notional cap, quantity cap, market order gate in
  `tradehub/policy.py`)
- Prompt injection that causes the AI layer to construct or submit an unintended order
- Credential exposure via logs, audit DB, API responses, or error messages
- SSRF via the Tiger gateway calls if `TRADEHUB_BIND_HOST` is changed from `127.0.0.1`
- Privilege escalation via the Telegram bot (unauthorized chat IDs bypassing
  `TELEGRAM_ALLOWED_CHAT_IDS`)

## Out of Scope

- Vulnerabilities in Tiger Brokers' own infrastructure or SDK
- Denial-of-service against a publicly exposed instance (TradeHub is not designed for public
  exposure; running it on a public interface is a misconfiguration, not a bug)
- Vulnerabilities in third-party dependencies that have no published CVE and no known exploit path
  in this project's usage
- Social engineering or phishing attacks against the operator

## Reporting a Vulnerability

**Do not open a public GitHub issue for security bugs.**

Email **joncallim@gmail.com** with the subject line `[TradeHub Security] <short description>`.

Include:

1. A description of the vulnerability and the component affected
2. Steps to reproduce (minimal reproduction case preferred)
3. Potential impact
4. Any suggested mitigation

You will receive an acknowledgement within **3 business days**. If the report is confirmed, a fix
will be targeted within **14 days** for high-severity issues and **30 days** for lower-severity
issues.

Reporters who follow responsible disclosure will be credited in the fix commit unless they prefer
to remain anonymous.

## Supported Versions

This project is currently pre-release (v0.x). Security fixes are applied to `main` only; there are
no maintained release branches yet.

## Security Design Notes

The key controls in the current codebase are documented in the
[threat model](docs/threat-model.md). The short version:

- The API requires a bearer token on every endpoint.
- `dry_run=true` is the default; no live orders are placed without explicitly setting
  `TRADEHUB_DRY_RUN=false`.
- Every order preview produces a single-use, TTL-bound confirmation token that must be presented
  to `/orders/submit`.
- The policy engine runs on every preview and again on submit.
- Every event (preview, submit, block, error) is written to a SQLite audit log.
- The server binds to `127.0.0.1` by default.
