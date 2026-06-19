# Changelog

## 0.1.0 - Unreleased

- Harden API bearer-token comparison, credential masking, and token strength validation.
- Make confirmation tokens atomic and retryable after transient live-placement failures.
- Require confirmation for every order submit path.
- Limit Release 1 order support to USD-denominated limit orders.
- Sanitize upstream broker errors returned to clients and stored in audit payloads.
- Add account-read Telegram commands and fail-closed Telegram chat authorization.
- Add CI, Dependabot, dependency audit, dependency lockfile, license, and packaging metadata.
- Expand tests for policy, authentication, audit token lifecycle, submit flows, and Tiger gateway
  wiring.
