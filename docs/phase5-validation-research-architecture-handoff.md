# TradeHub V2 Phase 5 — Research & Architecture Handoff

Status: **canonical implementation handoff for #38**

Baseline reviewed: `main @ 757fd51`

Primary issue: #38 — Historical & forward validation

This document does the research/architecture work before implementation. Hermes/DeepSeek should treat it as the starting design, challenge it only where the live code/data disproves an assumption, and keep any replacement simpler than what it replaces.

---

## 0. End state

TradeHub is not meant to become a quant-research laboratory that happens to trade.

The desired end state is a **simple autonomous investment system**:

1. observe the market and authoritative evidence;
2. run a small set of deterministic Hunters;
3. use models only where interpretation materially helps;
4. make deterministic portfolio/risk decisions;
5. execute only inside the human-defined constitution;
6. report actual profits, losses and performance to the owner every day/week in plain language.

Phase 5 therefore has two jobs:

- prove which research components actually add useful information; and
- create the validation/performance spine that lets TradeHub improve without fooling itself.

**Every complex component must earn its place. If equal-weight Hunters beat the committee stack, simplify the stack.**

The future autonomous mode should be an operating mode on top of the same deterministic machinery, not a second trading system.

---

# 1. Decisions already made by this architecture pass

These are the default decisions for #38 unless implementation evidence forces a change.

1. **No optimiser or parameter-search engine in Phase 5.** Fixed baselines/ablations only.
2. **No self-modifying production parameters.** #38 observes and recommends; it does not promote.
3. **No mass historical LLM committee replay.** It is expensive, model-version-dependent and creates a new data-mining surface.
4. Historical validation is strongest for deterministic Hunters/scoring; committee quality is evaluated from existing telemetry, bounded blind replay only if necessary, and forward tracking.
5. **3m and 6m are co-primary research horizons.** 1m and 12m are secondary diagnostics. This matches the existing 3–12m thesis hypothesis without pretending weekly reporting means a short-term strategy.
6. Core historical cross-sectional evaluation uses a **monthly PIT grid** to reduce duplicate observations and serial dependence. Event/informed-activity Hunters may additionally receive event-time evaluation.
7. Entry convention for historical outcome labels is the **next eligible US session**, never a price already known only after the decision timestamp.
8. The primary predictive statistic is **cross-sectional rank information coefficient (Spearman IC) by observation date**, aggregated across dates, rather than one pooled regression over thousands of correlated security-rows.
9. Also report top-v-bottom spread, pass-v-fail spread, downside/tail outcomes and a simple equal-weight portfolio simulation.
10. Uncertainty is dependence-aware. Use a deterministic block/stationary bootstrap over time blocks (or a simpler demonstrably equivalent method), not IID row bootstrap.
11. Every attempted variant is append-only. A bad result remains visible.
12. A sealed OOS regime is chosen **from coverage dates, not performance**, before the final variant is evaluated.
13. `research.db` is read-only to historical evaluation. Experiments/results live in a separate `experiment.db`.
14. Prefer one new analytical dependency at most: **DuckDB**, if it genuinely simplifies broad joins/analytics. Do not add pandas/NumPy/SciPy merely by convention.
15. Actual-account P&L reporting should use **Tiger account analytics as the accounting source of truth**, not a hand-built TradeHub accounting engine.

---

# 2. What the repo already gives Phase 5

Phase 5 should reuse rather than recreate these foundations.

## 2.1 PIT identity, universe and evidence

The research schema already has stable security IDs, identity events, PIT universe membership, evidence provenance and correction/supersession chains.

`EvidenceStore.historical()` already excludes unknown/unverified publication times and resolves visible correction chains using public availability. That should remain the authoritative semantics for historical evidence.

Important rule:

> Event time is not publication time. Historical validation asks what TradeHub could have known, not what was later discovered to have happened.

## 2.2 Broad screen telemetry

All six current Phase-1 Hunters are deterministic and registered:

- valuation
- inflection
- quality
- informed_activity
- event
- momentum

`ScreenResult` retains:

- raw features;
- pass/fail;
- sufficient-data status;
- evidence IDs;
- reason codes;
- confidence;
- data quality;
- deterministic hashes.

This is exactly the substrate required to evaluate **failed securities and never-selected securities**, not merely winners.

## 2.3 Deterministic scoring

The current conviction score is deliberately computed from screens/evidence; model assessments do not directly set conviction.

This matters for Phase 5.

The historical question is not simply:

> “Does the LLM score outperform the Hunter score?”

There is no LLM conviction score in the production architecture.

Committee incremental value instead appears through:

- committee agreement used in downstream eligibility;
- material disagreement detection;
- Red Team/Arbiter escalation;
- missing/contradictory evidence surfacing;
- thesis/falsification quality;
- potentially avoiding false positives or identifying risks.

## 2.4 Portfolio plane

Phase 3 already provides deterministic state, persistence, risk and proposal semantics. Phase 5 can simulate these secondarily, but **signal quality should be evaluated before portfolio sizing** so a bad sizing policy cannot hide a useful signal (or vice versa).

## 2.5 Experiment scaffolding exists but is in the wrong storage plane

Phase 0 already created concepts equivalent to:

- `experiment_run`
- `oos_evaluation_log`
- `sealed_holdout`
- snapshot manifests/versioning

and `ExperimentRegistry` can start/log attempts.

However, it currently operates through `ResearchDB`.

For #38, preserve the concepts but correct the boundary:

- historical/backtest reads frozen inputs;
- `research.db` is never mutated by backtesting;
- attempts/results are written to `experiment.db`.

Do not rewrite Phase-0 history just for tidiness.

---

# 3. External research conclusions that matter

The handoff intentionally takes the **less clever** conclusion from the literature.

## 3.1 Repeated backtesting can manufacture good-looking strategies

Bailey et al. show that selecting strategies after repeated historical simulation creates severe backtest-overfitting risk, and propose Probability of Backtest Overfitting methods for research processes with many trials.

Bailey & López de Prado's Deflated Sharpe Ratio similarly corrects apparent performance for selection/multiple-testing and non-normal returns.

Harvey, Liu & Zhu show that the finance “factor zoo” makes ordinary significance thresholds too permissive once many hypotheses have been tried.

**Architecture consequence:** Phase 5 does not start with CSCV/PBO, an optimiser, or a factor search. It prevents the problem first:

- a small predeclared variant set;
- append-only attempt log;
- walk-forward development;
- one sealed holdout;
- optional DSR/search-adjusted diagnostics only if there are enough strategy-return observations.

If a later adaptive layer starts proposing many variants, PBO/DSR becomes more valuable.

## 3.2 Overlapping horizons do not create thousands of independent observations

3m/6m/12m forward returns overlap heavily. Long-horizon literature warns that naive standard errors can drastically overstate precision when observations overlap.

The stationary bootstrap was specifically designed to build inference under weak serial dependence.

**Architecture consequence:**

- do not use raw security-row count as “N”;
- calculate cross-sectional statistics per date;
- report unique dates and securities separately;
- use block/dependence-aware uncertainty across dates;
- report an explicit effective-N diagnostic;
- label small horizon/sector slices LOW CONFIDENCE rather than overinterpreting them.

## 3.3 Delistings cannot silently disappear

The delisting-bias literature shows that omitting stocks after adverse delistings can materially inflate stock-selection results.

**Architecture consequence:** an outcome builder must never drop a name because it disappears.

A delisted name must end as one of:

- observed terminal payoff/return;
- known delisting with explicit outcome;
- **DELISTING_OUTCOME_UNKNOWN**, retained in coverage/censoring statistics and subjected to conservative sensitivity analysis.

Do not impute zero. Do not silently forward-fill. Do not pretend Tiingo necessarily has a CRSP-quality terminal delisting return.

## 3.4 Benchmark definitions change too

Kenneth French's Data Library provides long-run US market/factor series and now documents its 2025 transition from CRSP FIZ to CIZ return construction. Historical archives are available.

**Architecture consequence:** benchmark data itself is versioned input. Pin the downloaded benchmark artifact/hash in each snapshot/regime.

## 3.5 Tiingo raw vs adjusted prices

Tiingo EOD exposes raw and adjusted OHLCV plus dividend/split fields and states that its adjusted-price methodology incorporates splits/dividends using a CRSP-style methodology. Tiingo also notes evening corrections to EOD data.

The existing adapter wisely keeps adjusted fields under an audit-only namespace for decision-time research and emits explicit split/dividend events.

**Architecture consequence:**

- decision features continue to use PIT-safe raw evidence;
- outcome construction may use frozen adjusted prices strictly on the **future-label side**, or explicit raw+corporate-action reconstruction;
- adjusted outcome values are never exposed back to feature generation;
- cross-check a sample of adjusted-return labels against explicit corporate-action reconstruction.

## 3.6 Actual portfolio reporting should use broker analytics

Tiger's current OpenAPI account analytics exposes daily history including:

- asset value;
- daily P&L;
- daily P&L %;
- cash balance;
- gross position value;
- deposits;
- withdrawals.

Current account/position objects also expose realized and unrealized P&L.

GIPS guidance reinforces the general principle that performance calculations need consistent valuations and proper treatment of external cash flows; time-weighted periods are geometrically linked.

**Architecture consequence:** do not build a parallel brokerage ledger to tell the owner whether they made money. Normalize and reconcile Tiger's own account analytics, then render the report deterministically.

---

# 4. Data reality: what is likely evaluable

Hermes must run the actual coverage audit first. The table below is the expected shape, not permission to assume coverage exists.

| Component | Expected historical posture | Why |
| --- | --- | --- |
| Momentum | **EVALUABLE** if Tiingo history is backfilled | price-only, deterministic |
| Valuation | **PARTIAL/EVALUABLE** | requires historical market cap + PIT filings/facts |
| Quality | **PARTIAL/EVALUABLE** | requires PIT financial facts |
| Inflection | **PARTIAL** | needs sequential PIT reports and enough history |
| Informed Activity | **PARTIAL** | Form 4 historical coverage/backfill is the constraint |
| Event | **PARTIAL / sparse** | heterogeneous event types; event-time evaluation may be better than monthly grid |
| Current deterministic scoring | **EVALUABLE where screens/evidence exist** | scoring can be replayed without historical LLM calls |
| Committee/Red Team/Arbiter alpha | **LIKELY INSUFFICIENT HISTORICALLY** | model outputs/version history is new; do not fake a multi-year live track record |
| Committee operational value | **PARTIAL + forward-first** | agreement/disagreement/cost/escalation can be tracked now |

## 4.1 Current Tiingo bootstrap constraint

The repo's Tiingo adapter currently enforces:

- 50 hourly / 1000 daily request limits (with 10% reserve);
- a **450 unique-symbol rolling-month bootstrap ceiling**.

Do not remove this safety ceiling simply because Phase 5 wants more history.

Historical individual-stock outcome coverage therefore follows this order:

1. use already-stored PIT price history;
2. use already-frozen historical data if present;
3. backfill within the existing quota safely;
4. if the broad union exceeds current entitlement, pre-register a deterministic broad sample and label the evidence scope honestly;
5. do not buy another data plan without owner authorization.

A reasonable fallback is a hash-selected PIT-universe sample (not selected on future outcome), with all selection rules and coverage counts frozen before price retrieval.

The result then applies to the sampled universe, not “all US stocks”.

---

# 5. Proposed Phase-5 architecture

Keep four concepts only:

```text
READ-ONLY OPERATIONAL DATA
research.db
        |
        v
IMMUTABLE VALIDATION SNAPSHOT
PIT evidence + screens + identity/universe + frozen outcomes/benchmark
        |
        v
VALIDATION ENGINE
fixed baselines + fixed ablations + walk-forward/OOS
        |
        v
EXPERIMENT LEDGER
experiment.db (append-only attempts, metrics, holdout regimes)
```

And separately:

```text
LIVE FORWARD PREDICTIONS  ---> later outcomes
            |
            v
      forward tracker
```

No service bus. No distributed warehouse. No online feature store. No optimizer.

## 5.1 Snapshot boundary

Prefer extending the existing snapshot publication mechanism rather than building another competing one.

A Phase-5 snapshot manifest should freeze at least:

- source `research.db` schema/version;
- repo commit;
- evidence/universe date ranges and row counts;
- eligible security IDs;
- Hunter definitions/config hashes;
- scoring definitions/config hashes;
- identity/correction state;
- price/outcome source versions;
- benchmark artifact/hash;
- cost assumptions;
- snapshot content hash.

The immutable artifact may be SQLite or DuckDB-backed according to the existing snapshot implementation. The **manifest/table-content hashes**, not a convenient mutable file path, are the oracle.

## 5.2 `experiment.db`

Use a small separate SQLite database for governance/results.

Conceptual tables:

### `dataset_snapshot`

- snapshot_id
- manifest_json/hash
- artifact path/hash
- created_at
- coverage summary

### `evaluation_regime`

- regime_id
- snapshot_id
- development window
- walk-forward specification
- holdout window
- max horizon
- sealed_at
- opened/evaluated_at
- regime hash

### `experiment_attempt`

- attempt_id
- regime_id
- attempt_number
- name / variant
- config_json/hash
- status
- started/finished
- result hash/artifact
- failure reason

### `metric`

- attempt_id
- horizon
- segment (ALL/sector/etc.)
- metric name
- point estimate
- lower/upper interval
- row count
- unique securities
- unique dates
- effective-N

### `forward_prediction`

Append-only prediction/event row:

- prediction_id
- security_id
- as_of
- version/variant
- score/state
- evidence/config hashes
- horizon
- outcome due date

### `forward_outcome`

Append-only eventual result referencing a prediction. Never edit the original prediction once the future arrives.

Exact table names are implementation detail; these semantics are not.

---

# 6. Historical reconstruction design

## 6.1 Core monthly PIT grid

The canonical broad historical evaluation grid should be **monthly**, not M/W/F.

For each evaluation month:

1. choose a deterministic timestamp after the relevant monthly EOD data is knowable;
2. resolve PIT universe membership;
3. resolve PIT identity;
4. retrieve only evidence publicly knowable by that timestamp;
5. run the exact frozen Hunter version/config;
6. retain every sufficient/insufficient, pass/fail result;
7. compute deterministic scoring variants;
8. attach forward outcomes only after the feature table is frozen.

Why monthly:

- the intended thesis horizon is months;
- it reduces repeated near-identical observations;
- it lowers outcome dependence;
- it reduces backfill/API load;
- it is easier to audit.

The actual production M/W/F cadence remains unchanged. Forward validation uses actual production cadence.

## 6.2 Event-specific secondary grid

For sparse event/informed-activity signals, optionally add an event-time evaluation keyed to the first knowable public event timestamp.

Do not mix event rows into the monthly sample as if they were independent observations without labeling the evaluation mode.

## 6.3 Outcome labels

Use trading-session horizons:

- 1m = 21 sessions
- 3m = 63 sessions
- 6m = 126 sessions
- 12m = 252 sessions

Co-primary: 63 and 126 sessions.

The entry convention is conservative:

> first eligible session after the observation timestamp.

Prefer next-session open for trade-like outcomes. If the historical provider does not support a defensible open for that record, fall back to the next-session close and label the convention/version.

Exit uses the corresponding later session close.

Record:

- raw return;
- total return;
- benchmark return;
- benchmark-relative return;
- outcome status / missingness;
- entry/exit price IDs/timestamps.

A future outcome can never be a feature.

---

# 7. Predictive evaluation — keep it simple

## 7.1 Primary: cross-sectional rank IC

For each evaluation date and horizon:

- rank the signal/score cross-section;
- rank forward benchmark-relative returns;
- calculate Spearman rank correlation.

Then summarize IC **across dates**, not by treating every security-date as IID.

Report:

- mean/median IC;
- fraction of dates with positive IC;
- dependence-aware confidence interval;
- unique dates;
- unique securities;
- effective-N.

This answers whether stronger TradeHub signals generally rank future opportunities better.

## 7.2 Pass/fail and quantile spreads

For each Hunter:

- PASS vs FAIL forward excess return;
- top vs bottom quantile of its continuous diagnostic/confidence where meaningful;
- downside/tail result;
- hit rate (positive benchmark-relative outcome), secondary only.

A Hunter can be useful even if its average raw return is not spectacular if it reliably eliminates severe false positives or improves downside.

## 7.3 Portfolio simulation is secondary

Use one boring simulation to measure economic consequences:

- equal weight;
- fixed small number/top fraction selected before OOS;
- no optimizer;
- cash allowed;
- same rebalance convention across variants;
- same costs/slippage.

Do not let a clever sizing model become the reason a weak signal appears good.

Report:

- annualized/period return where appropriate;
- benchmark-relative return;
- drawdown;
- turnover;
- cost drag;
- volatility;
- Sharpe only with strong caveats and search/trial context.

---

# 8. Baselines

The full system must earn complexity against these fixed baselines.

## B0 — Broad market

Historical research benchmark:

- pinned Fama/French US market return artifact (or equivalent approved broad-market series).

User-facing live benchmark later:

- a configurable liquid US benchmark such as SPY/VTI from existing EOD data.

Do not silently swap benchmark definitions mid-experiment.

## B1 — PIT eligible universe

- equal-weight eligible sample/universe;
- cap-weight version only where historical PIT market cap is trustworthy.

## B2 — Simple transparent factor composite

No optimization.

Default simple composite:

- valuation
- quality
- momentum

Use an equal-weight cross-sectional rank of one predeclared, monotonic diagnostic per family (or the family's deterministic confidence×quality projection if raw-feature comparability makes this cleaner).

Freeze the mapping before OOS.

## B3 — Hunters only

Candidate quality from deterministic Hunters/funnel with **no committee gate**.

## B4 — Simple/equal scoring

Equal family contribution, no clever learned weights. Keep the current production scoring as the comparison variant, not the truth.

---

# 9. Ablations that answer real questions

Do not run every subset combination.

Pre-register only these architecture questions:

1. current scoring vs equal scoring;
2. all Hunters vs remove-one-Hunter (six runs);
3. current confluence vs no-confluence;
4. Hunters-only vs committee-gated decisioning where comparable data exists;
5. agreement gate on vs agreement gate absent;
6. Red Team/Arbiter operational utility — primarily forward, unless enough historical telemetry exists.

Ablation results must be able to recommend deletion.

---

# 10. How to evaluate the committee without lying to ourselves

The committee is too new to pretend it has a long historical live track record.

## 10.1 Do not mass-replay years of LLM calls

Reasons:

- today's model is not yesterday's model;
- prompt/provider versions differ;
- rerunning thousands of historical cases creates a new expensive search process;
- outcomes can inadvertently influence sampling/iteration;
- it would consume subscription/API capacity for weak causal evidence.

## 10.2 Historical committee evidence hierarchy

1. **Existing real stored assessments** — highest relevance, likely small N.
2. Existing model-call telemetry/cost/agreement/dispute outcomes.
3. A **small blind frozen historical sample** only if necessary:
   - sample selected by hash/strata before outcomes are inspected;
   - fixed prompt/model versions;
   - one pass;
   - result labeled `CURRENT_MODEL_HISTORICAL_REPLAY`, never “historical live performance”.
4. Forward tracking — becomes the long-term source of truth.

## 10.3 Committee usefulness metrics

Measure things it is actually allowed to affect:

- does low agreement identify later poor outcomes or tail risk conditional on deterministic conviction?
- do disputed candidates have worse outcomes?
- does Red Team materially change/clarify the dispute?
- does Arbiter resolve useful cases or merely add calls?
- cost/tokens per materially changed decision;
- false-positive/thesis-break rate;
- proportion of escalations that produce no decision-relevant change.

If Red Team/Arbiter rarely changes a decision and adds cost, recommend making it rarer or removing it.

---

# 11. Walk-forward and sealed OOS

## 11.1 Label-maturity rule

For horizon H, a development observation may only be used to choose a variant if its H-session outcome ends **before** the validation/holdout period begins.

This is the simple purge that prevents future-label leakage across a time split.

## 11.2 Walk-forward

Use expanding-history or fixed chronological folds, e.g. yearly/half-year validation windows according to actual coverage.

The purpose is stability, not model fitting.

Record each fold result independently.

## 11.3 Holdout selection

After the data-coverage audit, choose the latest contiguous matured period that gives meaningful coverage and freeze it **without seeing variant performance**.

A deterministic default is:

- label cutoff = snapshot end minus max evaluated horizon;
- holdout = latest ~20% of eligible evaluation dates before that cutoff, subject to a reasonable minimum duration;
- exact dates are recorded in `evaluation_regime` and never silently moved.

If coverage is too short, state `INSUFFICIENT DATA`; do not shrink the holdout until a preferred result appears.

Any post-holdout strategy edit means a new evaluation regime.

---

# 12. Multiple testing guardrails

Phase 5 should make overfitting difficult by construction.

Mandatory:

- every attempt logged;
- fixed variant list;
- fixed primary horizons;
- sealed OOS;
- no hidden retries under new names;
- no deleting failures;
- report number of tried variants.

Diagnostics where data permits:

- search-adjusted/Deflated Sharpe Ratio for strategy-return variants;
- performance decay from development to walk-forward to holdout.

Do **not** implement full combinatorial PBO/CSCV unless Phase 5 itself starts doing enough parameter searching to justify it.

---

# 13. Dependence-aware statistics

Do not add an econometrics framework.

V1 requirements:

1. calculate per-date cross-sectional metrics;
2. summarize time series of those date metrics;
3. use deterministic stationary/moving-block bootstrap with a recorded seed/block rule;
4. report interval + raw date count + effective-N;
5. sector slices require minimum support and otherwise print `LOW CONFIDENCE`.

For long overlapping horizons, explicitly report that 60 monthly rows do not mean 60 independent 12m outcomes.

Avoid making one p-value the product's decision oracle.

---

# 14. Costs and slippage

There is no need to pretend one cost number is truth.

Version a small fixed sensitivity set, for example:

- frictionless diagnostic (not investment evidence);
- moderate cost profile;
- conservative cost profile.

The architect may choose exact one-way bps/commission assumptions from documented Tiger fees/current execution evidence, but they must be frozen before holdout and never selected after seeing performance.

Report turnover separately.

As real PAPER/live executions accumulate, empirical implementation shortfall can replace generic assumptions.

---

# 15. Delisting/corporate-action rules

1. Do not build universes from today's ticker list.
2. PIT universe membership owns eligibility.
3. Stable security ID owns identity.
4. Corporate actions are applied in outcome construction.
5. A delisting never causes a row to disappear.
6. Unknown terminal value remains visible as censored/missing and is included in sensitivity/coverage statistics.
7. Report how many observations/horizons depend on unresolved delisting outcomes.

A backtest that improves when missing delists are silently dropped must fail acceptance.

---

# 16. Forward tracker

Forward tracking starts as soon as Phase 5 ships.

For every real future decision/screen state, record immutable prediction facts before the outcome exists.

Later append outcomes at 1m/3m/6m/12m.

This becomes the cleanest evidence for:

- committee usefulness;
- model/provider usefulness;
- actual portfolio-decision quality;
- regime drift;
- future #41 calibration.

Do not wait a year to build the tracker.

Phase-5 acceptance proves **the tracker**, not future profitability.

---

# 17. User-facing profit/performance architecture

This is the target that keeps the project practical.

## 17.1 Two performance concepts must stay separate

### Actual account performance

Source: Tiger broker/account analytics.

This answers:

> “How much money did I actually make?”

### Research/strategy performance

Source: Phase-5 forward tracker/backtest.

This answers:

> “Is the decision system demonstrating skill?”

Never substitute a backtest return for actual account P&L.

## 17.2 Do not reinvent brokerage accounting

Tiger currently exposes an account-analytics history with:

- daily asset value;
- daily P&L;
- daily P&L percentage;
- cash balance;
- gross position value;
- deposits;
- withdrawals.

Assets/positions additionally expose realized/unrealized P&L.

Recommended later read-only interface (likely #39 scope, not a reason to enlarge #38):

```text
account_performance(start_date, end_date)
    -> sanitized broker performance history
```

TradeHub should validate/reconcile it, not recalculate a shadow brokerage ledger.

A useful reconciliation is:

```text
flow_adjusted_profit ≈ end_asset - start_asset - deposits + withdrawals
```

and compare that with broker-reported period P&L. If the two materially disagree, report a reconciliation warning rather than picking the nicer number.

Period returns should chain broker-reported daily returns/geometric subperiod returns; do not claim formal GIPS compliance.

## 17.3 Daily report

Deterministic renderer; LLM prose optional and never owns arithmetic.

Example shape:

```text
TRADEHUB · DAILY

Today      +$123.45  (+0.42%)
WTD        +$310.20  (+1.06%)
NAV         $29,620
vs SPY      +0.18 pp today
Drawdown    -1.3%

BOOK
Cash 34% · Gross exposure 66% · 6 positions
Realized +$42 · Unrealized +$268 · Fees $3.20

TODAY
1 buy · 0 sells · 0 blocked
Best NVDA +$88 · Worst XYZ -$31

STATUS
No action recommended.
```

## 17.4 Weekly report

```text
TRADEHUB · WEEK

P&L        +$xxx (+x.xx%)
Benchmark  +x.xx%
Active     +x.xx pp
Since start +x.xx%
Max DD     -x.xx%
Fees       $x
Turnover   x%

DECISIONS
entries / adds / trims / exits / blocked

CONTRIBUTORS
best / worst positions

RESEARCH HEALTH
signals evaluated, data gaps, current validation verdict
```

Keep it terse. Never ask a model to calculate P&L.

## 17.5 Scheduling/end-state autonomy

Recommended final operating loop:

```text
AFTER US CLOSE
  ingest + reconcile data
  update Hunters/candidates
  conditional committee only where useful
  update portfolio decisions

NEXT EXECUTION WINDOW
  deterministic risk/policy check
  execute according to currently authorized mode

AFTER CLOSE DAILY
  actual P&L/performance report

WEEKLY
  account + strategy + benchmark summary
```

Autonomy should later be a mode:

- `HUMAN_APPROVAL` — current live posture;
- `PAPER_AUTO` — fully autonomous paper execution after validation;
- `LIVE_DELEGATED` — only after explicit owner authorization and evidence gates.

Do not entangle this mode with model adaptivity. **Autonomy does not require self-modifying weights.**

Recommended post-#38 priority:

1. finish #39/reporting/runtime ergonomics;
2. establish simple PAPER_AUTO + daily/weekly reporting;
3. only then invest heavily in #41 adaptive calibration if Phase-5 evidence says it is useful.

The system can be autonomous and simple without being self-rewriting.

---

# 18. Phase-5 evidence verdict

Closeout has two independent verdicts.

## A. Validation engine

- `PASS`
- `NOT ACCEPTED`

This is methodological/software trustworthiness.

## B. Investment evidence

- `SUPPORTS CURRENT COMPLEXITY`
- `DOES NOT SUPPORT CURRENT COMPLEXITY`
- `MIXED`
- `INSUFFICIENT DATA`

This is the empirical finding.

Do not conflate them.

## Provisional evidence decision rule

The full system should only receive `SUPPORTS CURRENT COMPLEXITY` when:

- it has mature data on at least one co-primary horizon and a credible sealed holdout;
- the full stack improves predictive/economic evidence over the simple baselines in holdout, not merely development;
- the direction is reasonably stable across 3m/6m or explicitly explainable;
- complexity does not buy tiny return improvement with materially worse turnover/drawdown/cost;
- the result is not dominated by one sector/date cluster.

`DOES NOT SUPPORT` is appropriate when the simpler system is as good or better across the co-primary evidence and/or complexity mainly adds cost.

`MIXED` is appropriate for genuine horizon/sector trade-offs.

`INSUFFICIENT` is appropriate when effective time support, PIT history, outcome coverage or delisting uncertainty is too weak.

Do not invent a universal p-value cutoff to turn this into a mechanical green light.

---

# 19. RA-05 — methodological acceptance

RA-05 should test the engine, not profitability.

Required deterministic contracts:

1. same frozen snapshot/config -> same result identity;
2. historical evaluation cannot write live `research.db`;
3. experiment writes go to `experiment.db`;
4. snapshot/manifest hash verifies;
5. future evidence is excluded from features;
6. unknown/unverified PAT excluded;
7. deliberately injected lookahead is caught;
8. pass AND fail screens included;
9. never-selected names included;
10. delisted name cannot silently disappear;
11. identity correction/ticker changes handled PIT;
12. future outcome label cannot be queried as feature;
13. adjusted outcome fields are inaccessible to feature path;
14. mandatory baselines generated;
15. fixed ablations generated;
16. failed/unflattering attempt remains in append-only log;
17. holdout dates/regime cannot silently change after opening;
18. overlapping outcomes do not report raw rows as effective-N;
19. dependence-aware interval deterministic under recorded seed;
20. transaction-cost assumptions versioned;
21. committee-vs-Hunters report produced without requiring historical mass LLM replay;
22. production strategy/config remains byte/semantically unchanged;
23. forward prediction is immutable before outcome;
24. outcome append does not mutate prediction;
25. RA-00..RA-04 remain PASS.

---

# 20. Suggested implementation packets

These are bounded work packets, not microservice boundaries.

## Packet A — coverage audit + storage boundary

- inventory actual historical coverage;
- classify EVALUABLE/PARTIAL/NOT_EVALUABLE;
- create/repair `experiment.db` boundary;
- freeze first validation snapshot;
- draft holdout regime from dates only.

Stop and report if the data is too thin for the planned claims. “Insufficient” is not a failure.

## Packet B — outcome builder + benchmark

- next-session entry semantics;
- 21/63/126/252-session labels;
- benchmark-relative returns;
- corporate-action/delisting handling;
- benchmark artifact pinning;
- lookahead canaries.

## Packet C — deterministic Hunter/scoring evaluation

- monthly PIT reconstruction;
- broad pass/fail outcome table;
- IC/spreads;
- equal/simple baselines;
- fixed Hunter/scoring ablations.

## Packet D — statistics + walk-forward/OOS

- time-block uncertainty;
- effective-N;
- walk-forward result table;
- sealed holdout run;
- attempt ledger;
- optional DSR diagnostic.

## Packet E — committee + forward tracker

- existing committee telemetry analysis;
- only bounded blind replay if necessary;
- forward prediction/outcome tracker;
- committee cost/utility report.

## Packet F — reporting contract

Do not broaden #38 into execution refactoring.

Define the actual-vs-strategy performance contract and, if it stays tiny/read-only, implement the broker-analytics normalization. Otherwise open/attach the concrete reporting work to #39.

End with a deterministic example daily/weekly renderer contract.

---

# 21. Review axes

Fresh independent review should attack:

## PIT / SURVIVORSHIP

- today's universe used historically;
- future filings/corrections;
- future-effective identity;
- adjusted-price leakage;
- dropped delists.

## STATISTICS

- overlapping horizons treated IID;
- one giant pooled sample hiding date dependence;
- tiny sector buckets;
- trial-count blindness;
- preferred-window cherry-picking.

## BASELINES

- benchmark given worse execution assumptions;
- full stack seeing information unavailable to baseline;
- ablation not truly removing component;
- committee comparison suffering selection mismatch.

## REPRODUCIBILITY

- mutable snapshot input;
- hidden experiment retry;
- seed not pinned;
- hash not covering important config/data.

## GOVERNANCE

- experiment writes into production;
- result auto-changing weights;
- recent paper P&L becoming a learning oracle;
- adaptivity sneaking into #38.

## SIMPLICITY

- can an entire module/metric/dependency be removed without reducing confidence?
- are we building a research platform instead of answering #38?

---

# 22. Explicit non-goals

Do not implement in #38:

- ML model training;
- Bayesian optimiser / hyperparameter search;
- automatic factor discovery;
- mass historical LLM replay;
- new data warehouse/service;
- online feature store;
- self-modifying production weights;
- live auto-submit;
- a custom brokerage accounting system;
- elaborate web dashboard if Telegram/plain-text report suffices.

---

# 23. Hermes execution directive

Hermes should now:

1. read #38, this document, `docs/adaptive-learning-principles.md`, and the current implementation;
2. verify the repo-specific assumptions above against live data/schema;
3. run the actual data-sufficiency audit before committing to a large implementation;
4. let one strong subscription-backed architect make the final minimal file/module plan;
5. open a #38 draft PR early;
6. implement Packets A–F sequentially with bounded parallelism where genuinely separable;
7. keep Codex review enabled when quota exists, otherwise use a genuinely independent subscription-backed reviewer;
8. accept negative investment evidence without tuning it away;
9. merge #38 only when the validation engine is trustworthy;
10. stop before #41.

The closeout must report separately:

```text
VALIDATION ENGINE — PASS / NOT ACCEPTED

INVESTMENT EVIDENCE —
SUPPORTS CURRENT COMPLEXITY /
DOES NOT SUPPORT CURRENT COMPLEXITY /
MIXED /
INSUFFICIENT DATA
```

Then recommend the **simplest** production research configuration supported by the evidence.

---

# 24. References used for this handoff

1. Bailey, Borwein, López de Prado & Zhu — *The Probability of Backtest Overfitting* (Journal of Computational Finance / SSRN): https://papers.ssrn.com/sol3/Papers.cfm?abstract_id=2326253
2. Bailey & López de Prado — *The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality*: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
3. Harvey, Liu & Zhu — *…and the Cross-Section of Expected Returns* (Review of Financial Studies / NBER): https://www.nber.org/papers/w20592
4. Politis & Romano — *The Stationary Bootstrap* (JASA): https://doi.org/10.1080/01621459.1994.10476870
5. Shumway — *The Delisting Bias in CRSP Data* (Journal of Finance): https://doi.org/10.1111/j.1540-6261.1997.tb03818.x
6. Shumway & Warther — *The Delisting Bias in CRSP's Nasdaq Data and Its Implications for the Size Effect*: https://doi.org/10.1111/0022-1082.00192
7. Kenneth French Data Library: https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html
8. Tiingo EOD documentation: https://www.tiingo.com/documentation/end-of-day
9. GIPS Standards Handbook — return/cash-flow principles: https://www.gipsstandards.org/standards/gips-standards-for-firms/gips-standards-handbook-for-firms/
10. Tiger OpenAPI — account information/analytics: https://quant.itigerup.com/openapi/en/python/operation/trade/accountInfo.html

These sources constrain methodology; they do not override the repository's PIT evidence, safety constitution or deterministic execution rules.
