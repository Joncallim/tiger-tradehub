# Agentic Implementation Policy

Status: canonical operating policy for TradeHub implementation work.

## 1. Entirely agent-run by default

TradeHub implementation, review, testing, acceptance, GitHub updates, and routine recovery are performed by agents. The human owner sets product/investment principles and handles only prerequisites that agents cannot safely resolve, such as credentials/2FA, external account actions, irreversible infrastructure choices, live-broker ambiguity, or changes to the investment/safety constitution.

A failed test is not a reason to stop for routine permission. Diagnose, escalate to the appropriate agent, fix minimally, rerun independently, and record evidence.

## 2. One-epic execution loop

For each authorized epic:

1. Read current `main`, the parent tracker, the epic, canonical architecture/review/threat-model docs, and relevant implementation/tests.
2. Decompose exploration where useful, but do not create coordination overhead for simple work.
3. Use a strong architecture/coding agent for material design or safety-sensitive changes.
4. Implement on a dedicated branch/PR with the smallest coherent change set.
5. Run deterministic unit/integration/CI checks.
6. Run independent adversarial review from a fresh context/model where practical.
7. Fix material findings and rerun review as needed; do not optimize for an arbitrary number of review rounds.
8. Run the epic's deterministic RA/FA acceptance pack with an agent that did not author the final fix.
9. Merge only when acceptance criteria are evidenced; close/update GitHub issues and the parent tracker.

Do not start the next epic unless the current authorization explicitly allows it.

## 3. Agent is operator, deterministic code is oracle

Low-tier agents may execute approved test/acceptance commands, collect structured artifacts, classify deterministic results, and report them. They must not change acceptance criteria, weaken policy, modify credentials, or patch source code merely to make their own test pass.

The agent that writes a material fix is not the final acceptance authority for that fix.

## 4. Subscription allocation before metered API allocation

At the start of each implementation/review cycle, Hermes should inspect the model routes actually available in its environment and classify them as:

- subscription/OAuth/included allocation;
- local/non-metered;
- metered paid API.

Routing priority for capable models is:

1. **Subscription-backed allocation first.** Burn available Claude/Claude Code and ChatGPT/Codex subscription capacity before equivalent paid API calls.
2. **Local/non-metered models** may handle mechanical orchestration or trivial work when appropriate, but should not displace useful subscription allocation simply to save included quota.
3. **Metered paid APIs are overflow**, used when subscription routes are unavailable/exhausted, lack a required capability, or an independent provider/model materially improves a safety-critical review.

For paid overflow, choose the cheapest model that is clearly adequate for the role. Do not spend paid tokens on broad fan-out or repeated reviews when a subscription-backed model can perform the same work.

For each major stage, record the chosen model/route and billing class in the implementation log or issue comment. Exact model names are intentionally not hard-coded here because available routes and subscriptions change.

## 5. Role guidance

- Architecture and safety-critical review: strongest suitable subscription-backed frontier model available first.
- Implementation: subscription-backed coding capacity first; parallelize only where modules are genuinely independent.
- Exploration: use cheaper subscription-backed agents when available; paid cheap models only as overflow.
- Deterministic acceptance: cheapest adequate agent, preferably subscription-backed; paid low-tier agents are acceptable overflow.
- Independent review: use a different model/context where it adds real orthogonality, but do not purchase diversity for its own sake.

## 6. Cost and retry discipline

- Bound retries and model fan-out.
- Reuse deterministic artifacts instead of asking multiple models to rediscover the same facts.
- Escalate only the failing component, not the entire epic.
- A model/provider failure should fall through to the next suitable route automatically where safe.
- Paid fallback is permitted without human intervention for an already-authorized epic, but the reason for paid fallback must be recorded.

## 7. Human stop conditions

Stop and ask the owner only when at least one of these is true:

- credentials, 2FA, subscription/account activation, or an external account action is required;
- an action may affect a LIVE brokerage account outside an already-approved guarded test;
- an irreversible/destructive infrastructure action is required;
- a material product/investment/safety-constitution decision is genuinely open;
- safe progress is impossible because required evidence is unavailable or contradictory.

Do not stop for ordinary code defects, failing tests, merge conflicts, package incompatibilities, or review findings that agents can resolve safely.
