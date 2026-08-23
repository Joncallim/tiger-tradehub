---
name: Functional acceptance test
about: Define an agent-runnable TradeHub acceptance pack without embedding arbitrary shell instructions
title: "FA-XX — "
labels: testing
assignees: ''
---

Parent: #23

## Pack contract

- Pack ID: `FA-XX`
- Environment: `offline | local | paper`
- Depends on: <!-- issue numbers / pack IDs -->
- Default tester: `DeepSeek V4 Flash`
- Terminal states: `PASS | FAIL | BLOCKED | ESCALATE`

Executable behavior belongs in the versioned acceptance runner/pack definition, **not** in free-form issue prose. Hermes/DeepSeek should invoke the pack by ID and report the structured runner result.

## Purpose

<!-- What deployed behavior does this pack prove that existing pytest/CI does not already prove? -->

## Safety invariants

- [ ] No production policy/credential/test-criterion modification is permitted to make the pack pass.
- [ ] Any broker write requires the `paper` environment and broker-proven `accountType=PAPER`.
- [ ] Unknown/ambiguous safety state returns `BLOCKED` or `ESCALATE`.
- [ ] Public artifacts contain no secrets, account identifiers, or raw confirmation tokens.

## Assertions

<!-- Deterministic assertions owned by the runner. Avoid duplicating existing unit tests unless the runtime/MCP boundary itself is under test. -->

- [ ] 

## Acceptance criteria

- [ ] `tradehub-acceptance run FA-XX --json` produces a schema-valid result.
- [ ] The low-tier tester can report the result without technical or financial judgment.
- [ ] A source-code change, if required, is escalated and independently rerun after the fix.

## Out of scope

<!-- Explicitly list tempting adjacent tests that do not belong here. -->
