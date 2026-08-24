# Adaptive Learning Principles

Status: long-term design constraint; **not permission to implement adaptive production weights in current V2 epics**.

## 1. Target operating model

TradeHub should eventually become self-calibrating inside a human-defined investment constitution.

The owner defines the durable principles: permitted markets/instruments, risk appetite, concentration limits, evidence standards, execution authority, leverage/shorting policy, and other hard safety/mandate boundaries.

TradeHub should increasingly own the research tactics beneath those boundaries: which signals are useful, which evidence combinations matter, which model is best for a role, which horizons fit a thesis type, and which research components should be downweighted or retired.

## 2. Learn primarily from the external market, not recent portfolio P&L

The principal learning dataset is the broad point-in-time market history TradeHub observes across the eligible universe, including securities it never buys.

Recent portfolio results are a validation stream, not the main teacher. A handful of monthly trades is too small and too selected a sample to justify changing a strategy.

Future calibration should therefore use broad cross-sectional and time-series observations such as:

- Hunter raw features and pass/fail results across the PIT-eligible universe;
- subsequent 1m/3m/6m/12m outcomes;
- sector/industry and market-regime context;
- source reliability and freshness;
- model/role performance;
- thesis-break and false-positive rates;
- turnover, drawdown, liquidity, and transaction-cost consequences.

## 3. Hunters are provisional feature generators

No current Hunter rule, threshold, feature weight, evidence prior, committee shape, or model role is sacred.

Phase 1 and later stages must preserve enough information to evaluate both successes and failures without selection bias. In particular, deterministic screen output should retain versioned raw features/reason codes/evidence references for **all evaluated securities**, including those that fail the screen, while the candidate funnel remains free to forward only a bounded subset to expensive model analysis.

A later learner must be able to ask: "What happened to everything we screened, not merely what we selected?"

## 4. Different adaptation speeds

- **Fast:** new evidence, prices, estimates, filings, events, liquidity/volatility state.
- **Medium:** source credibility, model-role calibration, sector/horizon usefulness after adequate samples.
- **Slow:** strategy feature weights, interactions, candidate thresholds, committee routing, and horizon assumptions.
- **Governed / non-self-adaptive:** hard risk limits, permitted asset classes, live/paper authority, leverage/shorting permissions, execution safety, credential boundaries.

Production research parameters should not chase monthly performance.

## 5. Promotion, not self-rewrite

Candidate changes should move through a controlled promotion ladder:

1. discovery from broad market evidence;
2. shadow calculation;
3. point-in-time historical replay;
4. walk-forward evaluation;
5. sealed out-of-sample evaluation;
6. paper-forward validation;
7. production promotion only when the change is within delegated authority;
8. otherwise proposal to the owner.

Every promoted variant must be versioned and reversible. Failed and rejected variants remain in the experiment/evaluation log.

## 6. Anti-overfitting requirements

Adaptive changes should require, as appropriate:

- minimum effective sample sizes;
- shrinkage toward long-run priors;
- sector/horizon stratification where justified;
- multiple-testing controls and append-only experiment logging;
- frozen/sealed OOS windows;
- maximum parameter change per review cycle;
- comparison with simple baselines and the current production version;
- rollback when forward evidence deteriorates.

Recent monthly results alone are never sufficient evidence for a production strategy change.

## 7. External analysis is evidence context, not authority

Primary market/fundamental data and authoritative public disclosures remain first-class evidence.

External analyst commentary, research notes, media, social discussion, and model-generated interpretations may contribute context or candidate hypotheses, but they must retain provenance and must not silently become facts or executable instructions.

## 8. Adaptive authority boundary

Within future delegated limits, TradeHub may eventually auto-adjust or auto-promote items such as:

- model routing;
- source/model credibility estimates;
- candidate budgets within bounded ranges;
- feature/evidence weights within governed ranges;
- conditional use of red team/arbiter roles;
- research thresholds validated out of sample.

It may not autonomously redefine the investment constitution, including hard risk limits, permitted instruments/markets, leverage/shorting, or execution approval authority.

## 9. Immediate implementation consequence

Current V2 epics should **prepare the telemetry and versioning needed for future adaptation but must not implement self-modifying production behavior yet**.

Phase 1 must retain broad-market screen observations. Phase 2 must version scoring/model contracts. Phase 5 must evaluate the whole screened population and simple baselines. A later post-validation epic may implement adaptive calibration only after these foundations are empirically trustworthy.
