# Tiger TradeHub Marketing Input Program

Date: 2026-06-17

## Purpose

Tiger TradeHub should be marketed as a portfolio-grade security and AI-enabled coding project, not
as a money-making trading product.

The program goal is to attract high-quality input from people who can improve the project:

- Security engineers who can review the threat model and control design.
- AI tooling builders who understand MCP, agent tools, and approval UX.
- Broker API users who can test dry-run and paper-account workflows.
- Hiring managers, engineering peers, and open-source maintainers who can assess the project as a
  serious portfolio artifact.

The core message should stay conservative:

> Tiger TradeHub is a local-first, guarded bridge from AI assistants to Tiger Brokers. It keeps
> broker credentials local, defaults to dry-run, validates order intent, requires explicit
> confirmation, and records an audit trail. The project is an exploration of safer AI-assisted
> brokerage tooling, not investment advice or an autonomous trading bot.

## Inputs Reviewed

Project documentation and metadata:

- `README.md`: current product overview, safety model, Claude setup, REST calls, ChatGPT Actions
  caveat, Telegram commands, and Tiger source links.
- `docs/comparable-projects.md`: scan of adjacent Tiger, MCP, AI trading, Telegram, and trading
  platform repositories.
- `tiger_tradehub.egg-info/PKG-INFO`: generated package metadata with an earlier README snapshot.
- `pyproject.toml`: project description, optional MCP and Telegram extras, CLI entry points.

Supporting code pass:

- `tradehub/app.py`: FastAPI surface, bearer auth, health, preview, submit, cancel.
- `tradehub/policy.py`: symbol allowlist, max quantity, max notional, market-order block, dry-run
  warning.
- `tradehub/audit.py`: SQLite confirmations and audit events.
- `tradehub/config.py`: local bind default, dry-run default, approval default, token, limits, Tiger
  credentials, Telegram allowlist.
- `tradehub/mcp_server.py` and `tradehub/telegram_bot.py`: assistant and chat surfaces that call the
  guarded API rather than Tiger directly.
- `tests/test_policy.py`: current policy regression coverage.

External framing:

- MCP security guidance calls out risks such as session hijacking, local MCP server compromise, and
  the need to verify inbound requests.
- OWASP's LLM Top 10 highlights prompt injection, insecure output handling, supply chain risks,
  insecure plugin design, excessive agency, and sensitive information disclosure.
- The UK NCSC recommends treating LLMs that call tools as inherently confusable and constraining
  them with deterministic safeguards.
- OpenSSF Scorecard and SLSA provenance are useful public signals for open-source security posture
  and supply-chain maturity.

## Positioning

### Category

Security-conscious local AI tooling for broker API workflows.

Avoid positioning it as:

- A trading bot.
- An investment strategy.
- A public SaaS trading proxy.
- A system for autonomous trade execution.
- A guaranteed-safe AI trading system.

### Differentiation

TradeHub is narrower and more reviewable than broad AI trading platforms:

- Tiger-specific, using the official Tiger Python SDK.
- One policy engine shared by REST, MCP, and Telegram.
- Preview-first, submit-second order flow.
- Expiring confirmation tokens.
- Dry-run mode enabled by default.
- Local SQLite audit trail.
- Claude-first MCP interface backed by the guarded REST API.
- ChatGPT Actions possible only through controlled per-user deployments.

### Portfolio Story

The strongest portfolio narrative is:

1. I saw a risky new pattern: LLMs calling financial tools.
2. I built a local, constrained bridge instead of a raw proxy.
3. I documented the threat model, tested policy behavior, and invited security critique.
4. I used AI-enabled coding, but paired it with deterministic policy, tests, auditability, and
   conservative defaults.
5. I publicly iterated based on external review.

## Program Strategy

Run this as a 90-day input campaign with three loops:

- Trust loop: publish security artifacts, invite critique, close issues visibly.
- Demo loop: show the dry-run Claude and Telegram flows, ask users where setup or approval UX breaks.
- Build-in-public loop: share short technical notes that show AI-assisted development plus human
  security review.

The output is not revenue. The output is proof:

- Qualified review comments.
- Public issues and discussions.
- Security roadmap progress.
- Better docs and onboarding.
- Portfolio-ready posts, demos, and changelogs.

## Before Public Outreach

Complete these first so feedback lands in useful places.

### Required Repo Artifacts

- `SECURITY.md`: scope, responsible disclosure route, supported versions, no live-trading guarantee,
  no investment-advice statement.
- `docs/threat-model.md`: assets, trust boundaries, attacker stories, controls, residual risks.
- `docs/security-controls.md`: table of implemented, planned, and rejected controls.
- `docs/demo-script.md`: exact safe demo path using dry-run mode and no live credentials.
- `docs/ai-coding-notes.md`: how AI assistance is used, what is reviewed by humans, and what tests or
  checks gate changes.
- `.github/ISSUE_TEMPLATE/security-review.yml`: structured ask for threat-model or control review.
- `.github/ISSUE_TEMPLATE/ux-feedback.yml`: structured ask for onboarding and approval-flow feedback.
- `.github/ISSUE_TEMPLATE/broker-api-feedback.yml`: structured ask for Tiger/paper-account edge cases.
- `.github/dependabot.yml`: dependency update signal.
- GitHub Actions CI for tests and linting.

### High-Signal Security Checklist

The public checklist should be small and visible:

- Runs on `127.0.0.1` by default.
- Requires bearer token for all API routes.
- Keeps Tiger credentials in local environment variables.
- Defaults to `TRADEHUB_DRY_RUN=true`.
- Defaults to `TRADEHUB_REQUIRE_APPROVAL=true`.
- Blocks market orders by default.
- Supports symbol allowlist, max quantity, and max notional controls.
- Uses short-lived confirmation tokens.
- Records previews, submissions, cancellations, errors, and blocked requests.
- Uses one shared policy layer across REST, MCP, and Telegram.

### Add One Honest Risk Box

Use this in the README and launch posts:

> This is experimental software for local, paper-account-first workflows. Do not expose it publicly
> or use it for live trading until you have reviewed the threat model, configured strict limits, and
> verified the full flow yourself.

Honesty is a feature here. It will attract better reviewers.

## Audience Map

### Security Reviewers

What they care about:

- Threat boundaries.
- Token handling.
- Prompt injection and excessive agency.
- Local MCP server risk.
- Auditability.
- Supply-chain posture.
- Failure modes before live trading.

Primary ask:

> Can you review the threat model and tell me what control is missing before this should ever touch a
> live account?

Best artifacts:

- Threat model.
- Security controls table.
- Policy tests.
- Demo showing dry-run and blocked requests.

### AI Tool Builders

What they care about:

- MCP tool ergonomics.
- Confirmation UX.
- Whether the LLM can misunderstand or over-act.
- How tool schemas should expose dangerous actions.
- ChatGPT Actions deployment caveats.

Primary ask:

> Where should the assistant boundary be stricter, and what should the approval flow show before a
> user confirms?

Best artifacts:

- Claude demo script.
- MCP tool list.
- Preview and submit response examples.
- Planned tool schema improvements.

### Broker API Users

What they care about:

- Tiger credential setup.
- Paper-account behavior.
- Order preview fidelity.
- Market-specific edge cases.
- Cancellations and order state.

Primary ask:

> Can you run the dry-run or paper-account flow and tell me where the Tiger-specific assumptions are
> wrong?

Best artifacts:

- Quick start.
- Paper-account smoke test issue.
- Known limitations.

### Portfolio Audience

What they care about:

- Clear problem framing.
- Secure design judgment.
- Evidence of ownership.
- Good docs, tests, and iteration.
- Ability to use AI coding without delegating judgment to AI.

Primary ask:

> Does this project clearly demonstrate security-minded AI tooling work, and what would make it more
> credible to you?

Best artifacts:

- README.
- Architecture diagram.
- Changelog of feedback-driven improvements.
- Short demo video.
- Public issue/discussion threads.

## Campaign Phases

### Phase 0: Package The Ask, Week 0

Goal: make the repository safe to share.

Actions:

- Add the repo artifacts listed above.
- Add labels: `security-review`, `threat-model`, `agent-ux`, `broker-api`, `good-first-review`,
  `portfolio-feedback`, `paper-account`, `docs`.
- Create GitHub Discussions categories: Security Review, AI Tool UX, Broker API Feedback, Show and
  Tell.
- Pin a discussion: "What input would be most useful right now?"
- Add a one-screen architecture diagram to the README.
- Record a two to three minute dry-run demo.

Exit criteria:

- A stranger can understand the safety model in under two minutes.
- A reviewer can leave useful feedback without asking where to put it.
- The demo does not require live credentials or real trades.

### Phase 1: Warm Expert Review, Weeks 1-2

Goal: collect five to ten serious reviews before broader launch.

Actions:

- Ask 10 to 15 people directly: security peers, AI tooling builders, backend engineers, fintech
  developers.
- Send each person one narrow ask, not a generic "thoughts?"
- Open issues yourself for feedback received privately, with permission.
- Publish a short "review log" discussion every Friday.

Target inputs:

- Three threat-model critiques.
- Two MCP or AI-agent UX critiques.
- Two onboarding failures.
- One dependency or supply-chain improvement.
- One Tiger/paper-account edge case.

### Phase 2: Public Build-In-Public, Weeks 3-6

Goal: make the project discoverable to the right technical audience.

Channels:

- GitHub Discussions and Issues as the source of truth.
- LinkedIn for portfolio narrative.
- Personal blog or dev.to for technical write-ups.
- Relevant AI coding, MCP, Python, and security communities where self-promotion is allowed.
- OpenSSF and OWASP community spaces for security-learning discussion, framed as a request for
  critique rather than a product launch.

Weekly cadence:

- Monday: publish one artifact or release note.
- Tuesday or Wednesday: share one concise technical post.
- Thursday: send five targeted review requests.
- Friday: publish "what changed because of feedback."

Content themes:

- "Why I made a guarded broker bridge instead of a raw MCP trading tool."
- "Designing a two-step confirmation flow for risky AI tools."
- "What prompt injection means when an LLM can call financial APIs."
- "Dry-run defaults and policy checks as portfolio-grade engineering."
- "Using AI coding assistants while keeping security decisions deterministic."

### Phase 3: Credibility Milestones, Weeks 7-10

Goal: convert attention into portfolio proof.

Actions:

- Close or document the top security issues found in Phase 1 and Phase 2.
- Add OpenSSF Scorecard and publish the result.
- Add CodeQL or equivalent static analysis.
- Add dependency review and Dependabot.
- Add coverage for confirmation token expiry and replay behavior.
- Add a paper-account smoke-test plan, even if it remains manually gated.
- Add screenshots or terminal captures for blocked order, dry-run submit, and audit event.

Public update:

- Publish "Security review v0.1: what reviewers found and what changed."
- Include unresolved risks. That makes the portfolio stronger, not weaker.

### Phase 4: Portfolio Launch, Weeks 11-12

Goal: package the story for hiring, networking, and future contributors.

Actions:

- Publish a case study:
  - Problem.
  - Constraints.
  - Architecture.
  - Security model.
  - AI-assisted development workflow.
  - Feedback received.
  - Changes made.
  - Remaining risks.
- Add the case study to a personal portfolio site or pinned LinkedIn post.
- Tag a `v0.1-security-review` release.
- Add a roadmap issue for `v0.2`.
- Thank reviewers in a `docs/reviewers.md` file if they consent.

## Outreach Templates

### Security Reviewer DM

Subject: Security critique request for a local AI-to-broker bridge

Hi NAME,

I built Tiger TradeHub as a local-first, dry-run-by-default bridge between Claude/MCP and Tiger
Brokers. I am treating it as a portfolio project around secure AI tool use, not as a trading product.

Would you be willing to review the threat model and answer one question: what control is missing
before this should ever touch a live brokerage account?

Repo: LINK
Threat model: LINK
Demo: LINK

No need for a full review. Even one sharp concern would help.

### AI Tooling Post

I am building Tiger TradeHub, a local-first guarded bridge from Claude/MCP to Tiger Brokers. The goal
is not autonomous trading. The goal is to explore how risky AI tools should be constrained:

- dry-run by default
- policy checks before preview
- short-lived confirmation tokens
- explicit submit step
- local audit trail
- no shared public trading endpoint

I would love feedback from MCP and AI tooling builders: what should the assistant be allowed to see,
and what should always stay in deterministic code or explicit user approval?

Repo: LINK
Demo: LINK

### Portfolio LinkedIn Post

I have been working on Tiger TradeHub, a portfolio project about secure AI-enabled coding and risky
tool use.

It is a local bridge from Claude/MCP to Tiger Brokers, but the design goal is intentionally
conservative: dry-run by default, no raw trading proxy, policy checks, explicit confirmation, and an
audit trail.

The interesting engineering question is not "can an AI place a trade?" It is "how do you design
guardrails when an AI can call tools with real-world consequences?"

I am looking for critique from security engineers, AI tooling builders, and broker API users.

Repo: LINK
Security review ask: LINK

### GitHub Discussion Prompt

Title: What would make Tiger TradeHub safer before any live-account use?

Tiger TradeHub is currently local-first and dry-run by default. The intended review question is:

> If this were your brokerage account, what would you require before enabling live trading?

Useful feedback:

- missing threat model entries
- missing policy checks
- approval-flow weaknesses
- token or credential risks
- MCP or ChatGPT Actions risks
- paper-account testing gaps
- audit trail gaps

Out of scope:

- trading strategy advice
- profit optimization
- requests to remove confirmation steps

## Feedback Triage

Use this scoring model for every suggestion:

| Score | Meaning | Examples |
| --- | --- | --- |
| P0 | Must fix before broader demo | credential leak risk, live order bypass, auth bypass |
| P1 | Must fix before live-account guidance | replay gap, unclear approval text, missing audit event |
| P2 | Portfolio credibility improvement | CodeQL, Scorecard, docs clarity, setup script |
| P3 | Nice to have | new broker support, UI polish, strategy features |

Bias toward security and credibility over scope expansion.

## Metrics

Track success by input quality, not revenue.

### Leading Metrics

- 20 targeted review requests sent.
- 10 qualified replies.
- 5 public issues or discussions opened by others.
- 3 security or AI-agent UX issues closed.
- 2 users complete dry-run setup.
- 1 user validates paper-account assumptions.
- 1 public case-study post published.

### Portfolio Metrics

- Clear README and demo.
- Threat model exists and is linked.
- Security controls table exists and is current.
- CI, lint, dependency update, and static analysis are visible.
- Feedback-driven changelog exists.
- At least one external reviewer is acknowledged.

### Anti-Metrics

Avoid optimizing for:

- Live trading volume.
- Profit claims.
- Removing approval friction.
- Broad broker support before Tiger is solid.
- Viral posts that attract users who want an autonomous trading bot.

## Roadmap Shaped By Marketing Inputs

Use feedback to drive this order:

1. Security docs and repo hygiene.
2. Policy tests for confirmation expiry, replay, and blocked submit paths.
3. Demo and onboarding improvements.
4. Paper-account smoke test.
5. Portfolio and buying-power read-only endpoints.
6. Configurable cross-channel approval, such as Telegram confirmation for assistant-originated
   previews.
7. More advanced deployment options only after the local-first story is mature.

Delay:

- Multi-user SaaS.
- Live-trading examples.
- Strategy automation.
- Multi-broker abstraction.
- Public ChatGPT Actions deployment templates.

## Source Links

- Tiger TradeHub README: `README.md`
- Comparable projects scan: `docs/comparable-projects.md`
- MCP Security Best Practices:
  <https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices>
- OWASP Top 10 for Large Language Model Applications:
  <https://owasp.org/www-project-top-10-for-large-language-model-applications/>
- NCSC prompt injection guidance:
  <https://www.ncsc.gov.uk/blog-post/prompt-injection-is-not-sql-injection>
- OpenSSF Scorecard:
  <https://openssf.org/projects/scorecard/>
- SLSA GitHub Generator:
  <https://github.com/slsa-framework/slsa-github-generator>
