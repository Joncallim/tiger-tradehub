# Tiger TradeHub documentation map

This directory contains the design, safety, validation, operations, and handoff material behind the top-level [`README.md`](../README.md).

For a first read, use this order:

1. [`../README.md`](../README.md) — what TradeHub is, how the complete system works, and current status.
2. [`forward-observation.md`](forward-observation.md) — **current operating state**: strategy freeze, evidence baseline, and the gates that must be met before investment logic changes.
3. [`v2-architecture.md`](v2-architecture.md) — canonical V2 technical architecture, service boundaries, data model, research/decision/execution separation, and failure semantics.
4. [`v2-architecture-review.md`](v2-architecture-review.md) — independent adversarial review of that architecture.
5. [`threat-model.md`](threat-model.md) — trust boundaries, attack surfaces, credentials, prompt-injection risks, and mitigations.
6. [`functional-acceptance-program.md`](functional-acceptance-program.md) — deterministic acceptance model, including exceptional PAPER broker-write gates.

## Current operating status

TradeHub is in **Forward Observation Mode**. The current production investment logic is frozen while genuine forward outcomes mature. The validation engine is operational, but investment evidence remains insufficient; the deployed broker path is dry-run. Strategy improvements should be recorded for later evaluation rather than applied to the frozen production baseline.

## Architecture and safety

| Document | Use it for |
|---|---|
| [`v2-architecture.md`](v2-architecture.md) | Full system architecture, service/process isolation, evidence model, committee, portfolio, and execution boundaries. |
| [`v2-architecture-review.md`](v2-architecture-review.md) | Hostile/orthogonal review findings and folded architecture corrections. |
| [`threat-model.md`](threat-model.md) | Security assumptions, trust boundaries, credential separation, prompt-injection containment, and broker-write risk. |
| [`../SECURITY.md`](../SECURITY.md) | Vulnerability scope, disclosure process, and short-form security controls. |
| [`../deploy/systemd/README.md`](../deploy/systemd/README.md) | Reference runtime isolation: separate execution/research users, environment files, state directories, and systemd hardening. |

## Validation and learning

| Document | Use it for |
|---|---|
| [`forward-observation.md`](forward-observation.md) | The active change freeze, forward-evidence baseline, adaptation checkpoints, and improvement log. |
| [`adaptive-learning-principles.md`](adaptive-learning-principles.md) | Long-term rules for evidence-driven adaptation; explicitly not authority to self-tune the current strategy. |
| [`functional-acceptance-program.md`](functional-acceptance-program.md) | Functional acceptance philosophy and deterministic evidence required before guarded changes are considered complete. |

The implementation of replay, outcomes, baselines, ablations, walk-forward evaluation, sealed holdout, look-ahead canaries, and forward tracking lives under [`../tradehub_research/validation/`](../tradehub_research/validation/).

## Agent and implementation policy

| Document | Use it for |
|---|---|
| [`agentic-implementation-policy.md`](agentic-implementation-policy.md) | Canonical rules for agent-run implementation, independent review, deterministic acceptance, cost discipline, and human stop conditions. |
| [`../AGENTS.md`](../AGENTS.md) | Repository-specific invariants, component ownership, risk triggers, and validation commands. |

Before editing investment-decision logic, check [`forward-observation.md`](forward-observation.md) first. During the current freeze, Hunter definitions/thresholds, scoring weights, candidate and portfolio thresholds, committee structure, investment horizon, return-driven model routing, and the risk constitution are intentionally unchanged except for genuine safety/operational defects.

## Code orientation

| Code area | Responsibility |
|---|---|
| [`../tradehub/`](../tradehub/) | Guarded Tiger execution core, policy, confirmation/audit lifecycle, broker gateway, client adapters, reconciliation, PAPER autonomy. |
| [`../tradehub_research/`](../tradehub_research/) | Research evidence, PIT universe, screening, committee, deterministic portfolio decisions, validation, and reporting. |
| [`../tradehub_research/adapters/`](../tradehub_research/adapters/) | SEC and Tiingo data boundaries. |
| [`../tradehub_research/hunters/`](../tradehub_research/hunters/) | Deterministic evidence-family screens. |
| [`../tradehub_research/committee/`](../tradehub_research/committee/) | Evidence packs, model-work routing, assessment validation, comparison, and scoring. |
| [`../tradehub_research/portfolio/`](../tradehub_research/portfolio/) | State, eligibility, sizing, risk, proposals, settlement, and briefings. |
| [`../tradehub_research/validation/`](../tradehub_research/validation/) | Historical/forward evaluation and evidence-quality machinery. |
| [`../tradehub/acceptance/`](../tradehub/acceptance/) | Execution-plane deterministic acceptance. |
| [`../tradehub_research/acceptance/`](../tradehub_research/acceptance/) | Research-plane deterministic acceptance. |

## Source of truth

When documents disagree, prefer the current implementation and tests for **what the repository actually does**, and the newest explicitly canonical/freeze document for **what it is currently allowed to do**. Do not use old phase handoffs or superseded review notes to override current code, current acceptance evidence, or the Forward Observation freeze.
