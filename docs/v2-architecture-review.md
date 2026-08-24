# Tiger TradeHub V2 — Orthogonal Review Record

Date: 2026-08-24
Companion to [`docs/v2-architecture.md`](v2-architecture.md). Method follows the existing Notion
Design Review & Divergence Register convention: for every material disagreement, state the
strongest case for, the strongest case against, and resolve only where one side clearly dominates
for V1. No fake consensus — items that don't resolve are marked `PROVISIONAL` or `OPEN`.

## A — Simplification

**For the proposed architecture's complexity:** a research/decision/execution three-plane split
with its own DB, its own process, a five-family Hunter contract, and a four-stage model committee
is a lot of moving parts for a personal single-user system.

**Against (why it's not over-built):** every piece maps to a requirement already fixed by the brief
or the canonical spec — the plane split is the brief's own boundary, the Hunter contract collapses
five bespoke platforms into one function signature, the committee stages are gated (Stage 3/4 only
fire on material disagreement, most candidates never reach them). The alternative — one undivided
process/module doing research, scoring, and execution — was tried on paper (§4 of the architecture
doc) and rejected specifically because it blurs the one boundary (credential isolation) the threat
model most needs.

**Resolved:** keep the plane split; the specific things that *were* cut are listed at the end of
this document ("Rejected Complexity").

## B — Database Necessity

**For a heavier store (Postgres) now:** relational integrity across many linked tables (evidence →
assessment → snapshot → proposal), growing to years of history, argues for a database built for
exactly that.

**Against:** V1's actual write pattern is a handful of batch jobs a few times a week plus occasional
committee-run writes — not concurrent multi-writer OLTP. SQLite in WAL mode (already proven in this
repo's own `audit.py`) handles that pattern, and its operational cost (a file, backed up with a file
copy) matches "one person can operate/debug/backup/restore/upgrade" far better than a Postgres
instance would for a workload that doesn't need Postgres's actual strengths (concurrent writers,
network access, replication) yet.

**Resolved for V1, PROVISIONAL beyond it:** SQLite is the system of record; DuckDB is a
library-only, on-demand analytical tool for backtests over a read-only snapshot; Postgres is
deferred. **Reopening condition:** measured (not assumed) WAL lock contention from genuinely
concurrent writers, or a demonstrated need for network-accessible multi-host access.

## C — Failure / Recovery at 3am

**For extra safety machinery (retries-with-backoff everywhere, health dashboards, alerting):** a
single operator debugging a broken overnight run wants maximum visibility.

**Against:** the acceptance program already proved a simpler pattern works for this exact operator:
deterministic, idempotent, independently-rerunnable stage functions with structured
PASS/FAIL/BLOCKED/ESCALATE-style results beat elaborate alerting, because the fix is almost always
"look at the last stage's result, fix the input, rerun the stage" — no partial-state cleanup needed
because every stage is idempotent and transactional (§18 of the architecture doc).

**Resolved:** reuse the acceptance-runner shape for research pipeline stages rather than building a
separate ops/alerting layer. Telegram-based failure notification is a plausible cheap addition later
(the bot infrastructure already exists) but is explicitly **OPEN**, not required for V1.

> **Superseded in part (Independent adversarial review, 2026-08-24 — see §M):** the claim that
> "every stage is idempotent and transactional" is false for the execution path — `tradehub/app.py`
> releases a confirmation token on any upstream exception even when the broker may have accepted the
> order, and `tradehub/audit.py` re-claims crash-abandoned tokens after 120s with no reconciliation,
> so a retry can duplicate a live order.

## D — Security: Compromised Model/Source/Hermes Session Causing an Unintended Trade

**For treating this as a hard, maybe unsolved problem:** LLMs are manipulable by adversarial text
in filings/news, and Hermes is a general-purpose agent with broad tool access — a sufficiently
clever prompt injection could try to get Hermes to call `/orders/submit` directly.

**Against — this is actually well-bounded, not open:** `tradehub-research`'s own HTTP client code
never implements a call to `/orders/submit` (§14) — there is no code path for the research plane to
submit, independent of what any model says. The only submit path is the existing, unmodified,
human-confirmed flow the execution core already defends with T1 (prompt injection) and T7 (policy
bypass) controls. Even if Hermes itself is fully compromised and tries to call the execution MCP
tools directly, it still needs a valid confirmation token (obtained from a real preview) and a human
to type "yes, submit this exact order" — nothing about V2 weakens that.

**Resolved:** the strongest available mitigation is architectural absence of a submit path in V2
code, not detection. Documented as the load-bearing invariant in §15 of the architecture doc and
carried into the threat-model update as new threats T8–T14, explicitly stated as *not* weakening
T1–T7.

> **Superseded (Independent adversarial review, 2026-08-24 — see §M):** the invariant was verified
> against V2's own code but never against the existing MCP surface — `tradehub/mcp_server.py`
> exposes `submit_order`, §21 wires Hermes to that MCP server, and §14 put raw confirmation tokens
> into the (evidence-text-exposed) Hermes briefing. A compromised Hermes session can therefore reach
> `submit_order`. The corrected invariant (committee/execution session separation, opaque token
> references, out-of-band retrieval, Telegram-only confirmation with re-rendered order, daily
> aggregate budget) is in architecture doc §15 and threat-model T16.

## E — Low-Tier Agent Operability (DeepSeek Flash-class routine ops)

**For requiring a stronger model to operate V2 routinely:** research pipeline concepts (evidence,
scoring, disagreement) sound like they need judgment.

**Against:** the *pipeline mechanics* (ingest, screen, build candidate pool, validate/store an
assessment, compute a score from already-submitted assessments) are all deterministic CLI/API calls
with structured results — exactly the shape the acceptance program already proved a cheap model can
dispatch and report on without judgment (§19). Only the model-committee *content* (writing the
actual neutral-analyst assessments) needs a capable model, and that's explicitly Hermes's job, using
whatever model Hermes is configured with — not the routine-ops model running the pipeline.

**Resolved:** low-tier agent handles pipeline dispatch/reporting via `RA-xx` packs, identical
contract to `FA-xx`. Model-committee content generation is out of scope for the low-tier operator by
design, not by policy discipline alone.

## F — Data Integrity (future data / duplicates / staleness)

**For extra defenses (checksums, external validation services):** financial data pipelines are
notorious for silent staleness and duplication bugs.

**Against over-engineering it:** the concrete failure modes are well-understood and already have
concrete, cheap mitigations in this design — append-only evidence with `supersedes_evidence_id` for
corrections (§7), content-hash/unique-key dedup (§18), and `public_available_time`-filtered queries
that make "using future data" a query bug, not a policy-discipline problem. No exotic tooling needed.

**Resolved:** the mitigations in §6/§7/§18 of the architecture doc are sufficient for V1. Revisit
only if a specific integrity failure is observed in practice.

> **Superseded (Independent adversarial review, 2026-08-24 — see §M):** the mitigations address
> duplicates and corrections but not timestamp provenance — `public_available_time` had no "unknown"
> representation, so sources that do not report a publication time silently corrupt the PIT ledger
> in either direction (lookahead or backfill lag), undetectable by the Epic 6 look-ahead check
> (which validates the predicate, not the timestamp's truthfulness). Fix: nullable
> `public_available_time` + mandatory `pat_provenance` enum, folded into architecture §6/§7.

## G — Quant / Overfitting (baking in today's beliefs)

**For trusting hand-tuned family weights sooner:** the canonical spec's priors (30/30/15/20/5-style)
came from real investment reasoning, not randomness.

**Against:** the divergence register already resolved this (item 11: "REJECTED as production
truth... equal/simple baselines are mandatory") and the evidence cited (backtest-overfitting
literature, correlated-LLM-error literature) is specific and current. Nothing in this architecture
review found a reason to reopen it — if anything, the risk is underestimated by teams that ship
their first backtest's winning weights as "the" weights.

**Resolved (reaffirmed, not new):** `scoring_version` (§6/§12) and the mandatory-baseline gate (§20)
are the concrete mechanisms preventing this. No family-weight change ships without beating an
equal-weight baseline out of sample, and every version is preserved for comparison, never silently
replaced.

> **Partially superseded (Independent adversarial review, 2026-08-24 — see §M):** `scoring_version`
> prevents silent overwriting; it does nothing about multiple testing — with cheap deterministic
> replayability, the N-th scoring version to beat the same frozen OOS window at p<0.05 is expected,
> and this section asserted otherwise. Fix: an `oos_evaluation_log` recording every attempt
> (including failures) per `(scoring_version, oos_window, baseline_set)`, and a second holdout
> window sealed at Phase 0, unsealed exactly once at the Phase 5 gate.

## H — Portfolio (giant hidden sector/factor bet)

**For deferring concentration limits until "we have real positions to worry about":** V1 will likely
hold very few positions initially, so a full factor-exposure model may be premature.

**Against:** the *interface* costs nothing to require now, and retrofitting a concentration check
after several ad hoc entries exist is exactly how hidden bets accumulate. The architecture (§13)
requires a concentration/correlation check step to exist structurally even though its numeric caps
are placeholder/configurable.

**Resolved:** seam required now; exact caps explicitly **OPEN** (see Open Decisions below) — do not
invent final numbers to look more finished than the evidence supports.

## I — Cost (components with ongoing cost, no proven value)

**For provisioning data vendors broadly upfront:** having estimates/13F/congressional feeds ready
avoids a mid-Phase-1 scramble.

**Against:** §8 already defers every paid vendor decision to the specific Hunter that needs it, and
free SEC EDGAR sources cover Phase 0/1's required fields entirely. Buying data before a Hunter is
built to consume it is exactly the "ongoing cost without proven value" pattern this pass is meant to
catch — and it's also a quant-integrity risk (§G): a vendor's specific historical-availability quirks
should inform the Hunter's design, not be bought blind.

**Resolved:** no vendor spend before Phase 1 defines the exact fields a specific Hunter needs (§8 is
already written this way). The committee's own LLM-call cost is naturally bounded by the funnel
(§10) and the gated Stage 3/4 escalation (§11) — most candidates never reach the expensive stages.

## J — Operations (one person operate/debug/backup/restore/upgrade/rollback)

**For a more elaborate ops story (dashboards, managed hosting):** research pipelines with LLM calls
and multiple data sources tend to accumulate operational surface.

**Against:** every choice in this doc was screened against exactly this property — bare
processes/systemd (no container orchestration to learn), file-copy backup (no cluster snapshot
tooling), `scoring_version` for logical rollback, idempotent rerunnable stages for recovery, and
zero new secret classes beyond one more local bearer token (§4, §21).

**Resolved:** no changes needed; this pass mainly confirms the design already optimizes for it.

## K — Scalability (thousands of securities + years of evidence, no early rewrite)

**For choosing Postgres/DuckDB-as-primary now to "future proof":** rewriting a persistence layer
later is real work.

**Against:** SQLite comfortably handles tens of millions of rows with proper indexing, which covers
"thousands of securities × years of point-in-time evidence" for V1's actual retention plan; and the
schema (§6) is fully relational, so a SQLite → Postgres migration if ever needed is a mechanical
schema/data port, not an application rewrite, *because* the application was never written against
SQLite-specific features.

**Resolved:** SQLite now, with the migration path documented and cheap specifically because the
schema doesn't lean on anything SQLite-only. Revisit only under the same trigger as item B.

## L — Migration (V2 without destabilizing Release 1)

**For extra caution (a staging branch, canary deployment, feature flags in the execution core):**
any change touching a production trading system deserves defense in depth.

**Against:** the strongest form of this defense is architectural, not procedural — V2 makes **zero**
changes to `tradehub/*.py`, `tests/`, `.env`, `pyproject.toml`. It cannot destabilize Release 1
because it is not a dependency of Release 1 in either direction except as an ordinary authenticated
HTTP client calling an already-supported endpoint (`/orders/preview`, callable by any bearer-token
holder today). No canary/feature-flag machinery is needed inside the execution core because the
execution core doesn't know V2 exists.

**Resolved:** §21 of the architecture doc is the migration plan; no further execution-core process
change needed.

## Current Open / Provisional Decisions (do not treat as resolved)

| Decision | Status | Why left open |
|---|---|---|
| SQLite vs. Postgres beyond V1 | PROVISIONAL | Reopen only on measured write contention or multi-host need (B, K). |
| Exact candidate/model-call budget | OPEN | Tune once Phase 1 ingestion volume is known (§10). |
| Exact position/sector/factor caps | OPEN | Interface required now (H); numbers require real portfolio evidence. |
| Primary live decision horizon (3–12mo hypothesis) | OPEN | Canonical spec §11 — needs 1/3/6/12-month backtest evidence before fixing. |
| Data vendors for estimates/13F/congressional/options | OPEN | Deferred to the specific Hunter that needs them (I, §8). |
| First sector-specific inflection feature packs | OPEN | Canonical spec §16 — start generic, earn complexity out of sample. |
| Amount of paper/out-of-sample evidence required before any Phase 6 automation | OPEN | Explicitly out of this document's scope; requires its own review when Phase 5 evidence exists. |
| Telegram/alerting for pipeline failures | OPEN | Plausible cheap addition (C); not required for V1. |

## Rejected Complexity (explicitly, so it isn't quietly reproposed later)

- LLM orchestration/provider-key management inside `tradehub-research` — Hermes does this (§11).
- Five bespoke per-family services/platforms — one Hunter contract, five implementations (§9).
- Internal scheduler daemon (Airflow/Celery/APScheduler-class) — cron + Hermes scheduling (§17).
- Postgres, Kafka, Redis, a vector DB, or Kubernetes for V1 — none are justified by V1's actual
  write/read pattern or single-host deployment target.
- Literal multiplicative scoring — rejected per canonical spec §9 and divergence register #5.
- Scout/Skeptic adversarial-by-default roles — superseded by neutral-analysts-first (§11, canonical
  spec §7).
- Modules-in-the-execution-process — rejected specifically for credential-blast-radius reasons (§4,
  §D above).
- Docker/container orchestration for a single-user single-host deployment — no isolation benefit
  over the existing bare-process + systemd pattern (§21).

## Proposed GitHub Epics (5–7, for Hermes to file)

Each epic below is sized to one phase (or one clearly cross-cutting concern). Dependencies follow
the phase order in §22 of the architecture doc.

### Epic 1 — V2 Phase 0: Evaluation Spine & Point-in-Time Evidence Foundation

**Purpose:** Stand up `research.db`, the security master, identity-event/universe-membership PIT
tracking, and the evidence ledger — the substrate everything else reads from.
**Boundaries:** No screening, no scoring, no model calls. Read/write only within `research.db`.
**Acceptance criteria:** schema migrations for `security`, `security_identity_event`,
`universe_membership`, `evidence_source`, `evidence_event`, `evidence_cluster`,
`experiment_run` scaffolding land; a PIT universe-reconstruction query is demonstrably correct
against a known historical date; append-only/supersession behavior is unit-tested; `RA-00`
qualification pack (mirroring `FA-00`) exists and passes offline.
**Dependencies:** none (first epic).
**Non-goals:** any Hunter, any scoring, any model integration.

### Epic 2 — V2 Phase 1: Deterministic Hunters & Candidate Funnel

**Purpose:** Implement the one Hunter contract (§9) and the five evidence-family screens plus
momentum/options confirmation modifiers; build the candidate funnel (§10); wire the V1-required
ingestion adapters (§8: SEC EDGAR filings/XBRL/Form 4/13F, prices, corporate actions).
**Boundaries:** Deterministic only — zero LLM calls in this epic.
**Acceptance criteria:** each Hunter is a pure function passing golden-file fixture tests; candidate
funnel produces a deduplicated, budgeted pool including holdings, event-triggered candidates, and a
rejected-control sample; ingestion adapters populate the Phase 0 evidence ledger with correct PIT
timestamps and dedup on rerun.
**Dependencies:** Epic 1.
**Non-goals:** model committee, scoring, portfolio states.

### Epic 3 — V2 Phase 2: Model Committee & Scoring Engine

**Purpose:** Evidence-pack API, structured `model_assessment` contract + validation (including
evidence-ID resolution rejection of hallucinated citations), deterministic claim comparison,
conditional red-team/arbiter gating, score/trajectory engine, `scoring_version` registry.
**Boundaries:** `tradehub-research` validates and stores; it does not call LLM providers (§11) —
Hermes does, via a companion skill defined in this epic.
**Acceptance criteria:** malformed/uncited assessments are rejected, not repaired; a candidate with
two agreeing neutral analysts produces a `score_snapshot` without invoking red team; a candidate with
material disagreement demonstrably triggers red team/arbiter; identical inputs + same
`scoring_version` reproduce an identical `score_snapshot` (replayability test); no-new-evidence rerun
does not move conviction.
**Dependencies:** Epic 2.
**Non-goals:** portfolio state transitions, proposals, execution.

### Epic 4 — V2 Phase 3: Portfolio State Machine & Paper Proposals

**Purpose:** State transition rules with persistence/hysteresis, sizing/concentration/correlation
engine, `trade_proposal` generation, M/W/F Hermes exception briefing.
**Boundaries:** Proposals are generated and stored; **no call to the execution core happens in this
epic** — proposals stop at `trade_proposal`, unlinked to any preview.
**Acceptance criteria:** state transitions respect persistence/cooldown and thesis-break bypass;
sell-reason codes are asymmetric per §13; concentration/correlation check blocks a synthetic
over-concentrated proposal in a test; Hermes briefing renders from real pipeline output in the terse
format specified in the canonical spec §13-equivalent.
**Dependencies:** Epic 3.
**Non-goals:** any execution-core interaction.

### Epic 5 — V2 Phase 4: Guarded Execution Integration

**Purpose:** Derive `OrderIntent` from an eligible `trade_proposal`, call the existing
`/orders/preview` as an authenticated client, record `execution_link`, and prove the boundary with a
new `RA-05`-equivalent acceptance pack (mirroring FA-05's "prove PAPER before any write" discipline).
**Boundaries:** Preview only. **This epic must not modify any file under `tradehub/`.** Submission
remains exclusively through the existing human-confirmed flow.
**Acceptance criteria:** a proposal reliably produces a valid confirmation token via the unmodified
`/orders/preview` endpoint; `execution_link` correctly references the token/order without a
cross-database foreign key; a PAPER-account acceptance pack exercises the full
proposal→preview→(existing)confirm→submit chain and reconciles by order ID; zero diffs in
`tradehub/*.py`, `tests/`, `pyproject.toml`, `.env` are present in the epic's PRs.
**Dependencies:** Epic 4.
**Non-goals:** auto-submission of any kind; loosening any existing policy check.

### Epic 6 — V2 Phase 5: Historical & Forward Validation

**Purpose:** Backtest engine over the read-only `research.db` snapshot writing only to
`experiment.db`, mandatory baseline/ablation comparisons, walk-forward evaluation, model/source
track-record population, paper-forward performance tracking.
**Boundaries:** Read-only against live research data; writes confined to `experiment.db`; never
touches `portfolio_state`/`trade_proposal`.
**Acceptance criteria:** backtest run against a frozen historical window reproduces the same result
on rerun (determinism); all five mandatory baselines (§20) are computed and compared for every
scoring-version evaluation; a deliberately-injected look-ahead bug (future `public_available_time`
data visible at `as_of`) is caught by an automated check, not manual review.
**Dependencies:** Epic 2 (can start backtesting deterministic Hunters before Epic 3/4 complete, per
§22's note that Phase 5 evidence-gathering can begin early).
**Non-goals:** any live automation decision (that's a future, separately-reviewed epic if Phase 5
evidence ever supports it).

### Epic 7 — V2 Cross-Cutting: Deployment, MCP Surface & Threat-Model Hardening

**Purpose:** `tradehub-research` systemd deployment (loopback, bare process, matching the execution
core's convention), the ~8-tool MCP surface (§16), the companion Hermes skill, and the
`docs/threat-model.md` T8–T14 additions.
**Boundaries:** Deployment/interface work only; no research logic lives here.
**Acceptance criteria:** `tradehub-research` starts/restarts under systemd with secrets injected, not
committed; MCP tool discovery works end-to-end from a Hermes session; the companion skill documents
the "untrusted evidence is data, not instructions" rule and the "proposals are presented, never
auto-confirmed" rule; threat-model update is reviewed and confirmed not to weaken any T1–T7 control.
**Dependencies:** can start in parallel with Epic 1 (deployment/MCP scaffolding doesn't need research
logic to exist yet) but should track Epics 2–5 for tool surface completeness.
**Non-goals:** any change to `.claude/skills/tiger-tradehub/SKILL.md` (not edited, per the brief) —
this is a **new**, separate companion skill.

## M — Independent Adversarial Review (2026-08-24)

Model: Claude Opus (no prior context, Read-only tools), orchestrated by Hermes cron, 2026-08-24.
Every citation was verified against the real `tradehub/*.py` (mcp_server.py, app.py, audit.py,
policy.py, tiger_gateway.py, config.py, models.py, telegram_bot.py, acceptance/service.py,
acceptance/packs/fa05.py, tests/test_audit.py) before folding — the review's code claims all
checked out.

**Verdict: MATERIAL REVISION FIRST.** Attacks A–L all run; A, E, I, J, K yielded nothing material
— the design is not over-built, the acceptance-runner-shaped ops story genuinely fits a low-tier
operator, and the vendor-deferral discipline in §8 is correct. The plane split, Hunter contract, PIT
schema shape, `scoring_version` registry, and backtest write-separation all held. Findings and where
the fixes landed (architecture doc = `docs/v2-architecture.md`):

1. **BLOCKER (D, A, L)** — A compromised Hermes session can reach `submit_order`
   (`tradehub/mcp_server.py:66`). §21 wires Hermes to that MCP server, and §14 put the raw
   confirmation token into the briefing — the context that just ingested adversarial evidence text.
   The invariant's "does not hold the confirmation token issuance authority" claim is false: preview
   *is* issuance, and §4/§14 grant the bearer token. → architecture §14 (opaque refs, out-of-band
   retrieval, `/confirm` re-render), §15 (corrected invariant), §21 (session separation),
   threat-model T16.
2. **BLOCKER (C)** — Submit non-idempotency: token released on any upstream exception
   (`app.py:230`) even when the broker may have accepted; crash-abandoned claims re-claimable after
   120s (`audit.py` `STALE_CLAIM_SECONDS`, asserted by `tests/test_audit.py:65`); `client_request_id`
   exists but is never sent to Tiger → a retry after a submit timeout can place a duplicate live
   order. → architecture §18 + Epic 5 gate: INDETERMINATE token state + reconciliation against
   `/account/orders` before reuse + broker idempotency key, as an independent core fix.
3. **BLOCKER (F, G)** — `public_available_time` had no "unknown" state: sources that don't report a
   publication time silently corrupt the PIT ledger in either direction, and the Epic 6 look-ahead
   check validates the predicate, not the timestamp's truthfulness. → §6/§7/§16 (`pat_provenance`
   enum, nullable field, backtest default filter, per-source histogram, `withdrawn` retraction).
4. **BLOCKER/MATERIAL (D, H)** — `accepted=True` hardcoded (`app.py:167`) regardless of Tiger's
   preview; `policy.py` side-blind while §13 emits SELLs (TRIM/EXIT); symbol allowlist fails open
   when empty (`policy.py:14`). → §13 (SELLs restricted to existing paper-account holdings; daily
   aggregate notional + order-count budget), §15.
5. **MATERIAL (L)** — "Zero changes to `tradehub/*.py`" falsified: no market-data endpoint exists
   (only `TradeClient` in the gateway; the only `QuoteClient` is in `acceptance/packs/fa05.py`);
   the FA-05 PAPER gate lives in the acceptance runner (`acceptance/service.py`,
   `TRADEHUB_ACCEPTANCE_PAPER_WRITE`), not on the submit path; `.env` sharing would put Tiger
   credentials in the research process environment (`config.py` `extra="ignore"` fails silently).
   → §8 prices fork, §19 RA-05 re-implementation, §21 `.env.research`.
6. **MATERIAL (G)** — Multiple testing: `scoring_version` records versions, not attempts against
   the same frozen OOS window; cheap replayability makes beating baselines by iteration inevitable.
   → `oos_evaluation_log` (attempts incl. failures) + sealed holdout window, Phase 5 requirement.
7. **MATERIAL (F, G)** — Confluence bonus over "distinct families" sharing one XBRL source = one
   dataset counted three times. → §12 (distinct `source_id`/cluster rule).
8. **MATERIAL/MINOR** — `?mode=ro` on a WAL DB still needs writable `-wal`/`-shm`; research DBs sat
   beside the private key in `data/`; `database is locked` (5s busy_timeout) silently drops paid
   model calls; `audit.py`'s per-call-connection pattern is not a transaction pattern. → §5/§20
   (snapshot-only backtest input, `data/research/` split), §18 (locked row, one-transaction-per-stage).
9. **MINOR (D)** — Telegram `/confirm TOKEN` posts the token without restating the order
   (`telegram_bot.py:79-89`); with V2 generating tokens in bulk, the operator confirms blind. → §14
   (re-render symbol/side/qty/limit + second affirmation).

Prior resolutions marked superseded above: **C** (idempotency claim false for the execution path),
**D** (invariant never checked against `mcp_server.py`), **F** (timestamp provenance not
considered), **G** (version preservation conflated with multiple-testing control). Everything else
stands. The four must-change items for adoption: (1) committee/execution session separation + no raw
tokens in committee context; (2) execution-core submit-idempotency fix before Phase 4;
(3) `pat_provenance` before Phase 0 lands any adapter; (4) daily aggregate budget + allowlist
fail-open resolution.
