from tradehub_research.validation.statistics import (
    cross_sectional_ic_by_date,
    effective_n,
    spearman_rank,
    stationary_bootstrap,
    summarize_ic_series,
)


def test_spearman_rank_tie_detection_is_ulp_tolerant():
    """Values that differ by one ULP (summation-order artifacts) are ties:
    exact float equality would rank 0.5 and 0.5000000000000001 as distinct,
    changing the IC across interpreters/compilers."""
    # The exact values that made CPython 3.11 (naive sequential sum) and
    # 3.14 (compensated sum) disagree: mean confidences from identical
    # inputs, differing in the last ULP.
    a = 0.5000000000000001
    b = 0.5
    ranks = spearman_rank([1.0, a, b, 0.0])
    assert ranks[1] == ranks[2]  # the two near-equal values share a rank
    assert ranks[0] == 4.0 and ranks[3] == 1.0
    # A REAL difference of 1e-9 must NOT be treated as a tie.
    ranks2 = spearman_rank([1.0, 0.5, 0.500000001, 0.0])
    assert ranks2[1] != ranks2[2]


def test_spearman_rank_with_ties():
    ranks = spearman_rank([10.0, 20.0, 20.0, 40.0])
    assert ranks == [1.0, 2.5, 2.5, 4.0]


def test_ic_perfect_positive():
    ic = cross_sectional_ic_by_date(
        {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0},
        {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4},
    )
    assert ic is not None
    assert abs(ic - 1.0) < 1e-9


def test_ic_perfect_negative():
    ic = cross_sectional_ic_by_date(
        {"a": 1.0, "b": 2.0, "c": 3.0},
        {"a": 0.3, "b": 0.2, "c": 0.1},
    )
    assert ic is not None
    assert abs(ic + 1.0) < 1e-9


def test_ic_needs_three_securities():
    assert cross_sectional_ic_by_date({"a": 1.0, "b": 2.0}, {"a": 0.1, "b": 0.2}) is None


def test_summarize_ic_series():
    summary = summarize_ic_series([0.1, 0.2, -0.1, 0.05, None])
    assert summary["date_count"] == 4
    assert summary["fraction_dates_positive"] == 0.75
    assert abs(summary["mean_ic"] - 0.0625) < 1e-9


def test_stationary_bootstrap_deterministic_under_seed():
    series = [0.05 + (i % 5) * 0.01 for i in range(60)]
    first = stationary_bootstrap(series, seed=42)
    second = stationary_bootstrap(series, seed=42)
    assert first == second  # identical intervals under the same seed


def test_effective_n_never_exceeds_date_count():
    assert effective_n(60, 126, 21) < 60
    assert effective_n(60, 21, 21) == 60
    assert effective_n(0, 126, 21) == 0.0
