# MCP vs Skill Decision

Date: 2026-06-17

## Decision

Build Tiger TradeHub as an MCP-backed local service first, then ship a companion Agent Skill that
teaches Claude how to use the TradeHub tools safely.

In short:

- MCP is the integration layer.
- The Skill is the operating manual.
- The REST API is the safety boundary both of them call.

## Why MCP Is The Core

Tiger TradeHub needs to connect an AI assistant to a local service that can read configuration, call
Tiger Brokers through the official SDK, enforce policy, create confirmation tokens, and record audit
events. That is exactly the shape MCP is for: connecting AI applications to external systems, data,
tools, and workflows.

The MCP server should expose only small, reviewable tools:

- `health`
- `preview_order`
- `submit_order`
- `cancel_order`
- future read-only tools such as `get_account`, `get_positions`, `get_buying_power`, and
  `get_orders`

The MCP server should not make security decisions by itself. It should call the guarded REST API,
where authentication, policy validation, confirmation handling, dry-run behavior, and auditing live.

## Why A Skill Still Matters

Agent Skills package reusable instructions, metadata, optional scripts, templates, and reference
materials. They are useful when Claude needs to follow a specific workflow repeatedly without the
user pasting the same instructions every time.

For Tiger TradeHub, the Skill should teach Claude to:

- Start with health checks and read-only data.
- Treat trade execution as the highest-risk final step.
- Never present TradeHub as investment advice.
- Show exact preview details before confirmation.
- Refuse to submit unless the user explicitly confirms the exact order.
- Prefer dry-run and paper-account workflows.
- Explain policy blocks clearly.
- Capture useful feedback for security and portfolio review.

This is especially useful because the market research points toward finance users wanting live data,
portfolio visibility, source-backed research, Excel-style analysis, and trust. The Skill can keep
Claude aligned with that workflow while the MCP server provides the actual tools.

## Why Not Skill-Only

A Skill by itself is not enough for this project.

Skills can package instructions and helper files, but they should not be the main place where broker
credentials, trading permissions, policy enforcement, confirmation tokens, or audit logging live.
Those controls need deterministic code and a narrow API boundary.

If Tiger TradeHub were only a writing/research workflow, a Skill might be enough. Because it touches
brokerage workflows, MCP plus the guarded REST API is the safer center of gravity.

## Why Not MCP-Only

MCP-only would work technically, but it leaves too much behavior implicit. Different users could ask
Claude to use the tools in unsafe or inconsistent ways.

The companion Skill turns the project into a more complete portfolio artifact:

- It documents the intended workflow.
- It makes the safety posture visible inside Claude.
- It gives reviewers a concrete artifact to critique.
- It helps keep demos and outreach aligned with the security story.

## Build Order

1. Keep the current REST API and MCP server as the core.
2. Add a project-level Claude Skill with safe operating instructions.
3. Add read-only REST and MCP endpoints before expanding execution features.
4. Add security artifacts: `SECURITY.md`, threat model, controls table, and issue templates.
5. Add tests for confirmation token expiry, replay, blocked submit paths, and audit events.
6. Later, package the Skill for Claude.ai upload if the project is shared beyond Claude Code.

## Current Scaffold

The repo now includes a project-level skill:

```text
.claude/skills/tiger-tradehub/SKILL.md
```

Claude Code can discover project-level skills from `.claude/skills/<skill-name>/SKILL.md`.

## Sources

- MCP overview: <https://modelcontextprotocol.io/docs/getting-started/intro>
- Agent Skills overview: <https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview>
- Claude Code skills: <https://code.claude.com/docs/en/skills>
- Claude for Financial Services repository:
  <https://github.com/anthropics/financial-services>
- Claude financial-services update:
  <https://www.anthropic.com/news/advancing-claude-for-financial-services>
