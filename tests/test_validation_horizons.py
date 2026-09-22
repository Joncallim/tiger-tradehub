"""One authoritative horizon definition: the two clocks cannot diverge.

The defect this file guards against: the DUE-DATE writer (forward_collector)
derived ~30/91/183/365 calendar days from a session horizon, while the
OUTCOME evaluator (ops/outcome_maturation) used its own hard-coded
{21: 40, 63: 105, 126: 210, 252: 420} day map -- two independently maintained
maps describing the same thing, so a prediction could be evaluated as if its
horizon were 40 sessions when it was due at 30.

Invariants asserted here:

1. exactly one module defines the horizon semantics, and no other module carries
   a hard-coded horizon -> calendar-day map (the source-of-truth test);
2. the due-date writer derives from that one definition;
3. the exit rule is "the N-th completed session after entry" -- shared, and it
   never substitutes a later bar when the horizon is immature;
4. `required_exit_session` counts sessions, not days (weekends/holidays skipped).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from tradehub_research.ops.market_calendar import count_sessions, is_session_day, next_session


# NOTE: `tradehub_research.validation.horizons` is imported INSIDE each test on
# purpose. The source-of-truth invariant below must be executable against a tree
# that does not yet contain it, so that it fails BEHAVIOURALLY (detecting the
# second clock) rather than merely erroring on an import.
def _horizons():
    from tradehub_research.validation import horizons

    return horizons


REPO_ROOT = Path(__file__).resolve().parents[1]

#: A mapping that pairs the 21-session horizon with a calendar-day count is the
#: exact signature of the defect class (any second clock).
SECOND_CLOCK_PATTERN = re.compile(r"\b21\s*:\s*\d{2,4}\s*,\s*\b63\s*:\s*\d{2,4}")


def _code_only(text: str) -> str:
    """The source with comments and string literals removed.

    Prose may *describe* the old defect (this module's own docstring does); only
    executable source can hold a second clock.
    """
    import io
    import tokenize

    pieces = []
    reader = io.StringIO(text).readline
    for token in tokenize.generate_tokens(reader):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        pieces.append(token.string)
    return " ".join(pieces)


def _python_sources() -> list[Path]:
    return sorted(
        path
        for path in (REPO_ROOT / "tradehub_research").rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_no_second_horizon_clock_exists_in_the_tree():
    # Deliberately import-free: this invariant must be checkable against any tree.
    """Source-of-truth invariant: one definition, no parallel day maps.

    Pre-fix this fails: `ops/outcome_maturation.py` carried
    `{21: 40, 63: 105, 126: 210, 252: 420}` while the collector derived due dates
    from a different formula.
    """
    offenders = []
    for path in _python_sources():
        if path.name == "horizons.py":
            continue  # the one authoritative definition
        text = _code_only(path.read_text(encoding="utf-8"))
        for match in SECOND_CLOCK_PATTERN.finditer(text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)!r}")
    assert offenders == [], (
        f"a horizon -> calendar-days map must not exist outside validation/horizons.py: {offenders}"
    )


def test_the_due_date_writer_derives_from_the_one_definition():
    horizons = _horizons()
    """The scheduling date and the evaluation horizon share their source."""
    from tradehub_research.validation import forward_collector

    for horizon in horizons.HORIZON_SESSIONS:
        assert forward_collector._outcome_due_date("2026-08-28", horizon) == (
            horizons.session_horizon_due_date("2026-08-28", horizon)
        )
    # The legacy approximation survives only to interpret rows already written.
    assert horizons.legacy_advisory_due_date("2026-08-28", 21) == "2026-09-27"


def test_the_advisory_and_exact_dates_track_each_other_closely():
    horizons = _horizons()
    """Legacy rows stay sensibly scheduled under the exact semantics."""
    for horizon in horizons.HORIZON_SESSIONS:
        for as_of in ("2026-01-15", "2026-05-29", "2026-08-28"):
            legacy = date.fromisoformat(horizons.legacy_advisory_due_date(as_of, horizon))
            exact = date.fromisoformat(horizons.session_horizon_due_date(as_of, horizon))
            assert abs((exact - legacy).days) <= 6, (
                f"h{horizon} as_of {as_of}: legacy {legacy} vs exact {exact}"
            )


def test_required_exit_session_counts_sessions_not_days():
    horizons = _horizons()
    """Weekends and market holidays are skipped (2026-06-19 is Juneteenth)."""
    entry = "2026-06-16"  # Tuesday
    exit_session = horizons.required_exit_session(entry, 21)
    assert is_session_day(date.fromisoformat(exit_session))
    # inclusive=False: the entry session itself is not one of the 21 after it.
    assert (
        count_sessions(date.fromisoformat(entry), date.fromisoformat(exit_session), inclusive=False)
        == 21
    )
    # 21 sessions from 2026-06-16 spans Juneteenth, so it is more than 21 days out.
    assert (date.fromisoformat(exit_session) - date.fromisoformat(entry)).days > 21


def test_required_exit_session_steps_over_a_weekend():
    """A Friday entry steps past the weekend AND Labor Day to the horizon."""
    horizons = _horizons()
    entry = "2026-09-04"  # Friday
    exit_session = horizons.required_exit_session(entry, 21)
    assert is_session_day(date.fromisoformat(exit_session))
    # The calendar span exceeds 21 days precisely because 2026-09-05/06 (weekend)
    # and 2026-09-07 (Labor Day) are not sessions and never count.
    assert (date.fromisoformat(exit_session) - date.fromisoformat(entry)).days > 21
    assert (
        count_sessions(date.fromisoformat(entry), date.fromisoformat(exit_session), inclusive=False)
        == 21
    )


def test_only_the_contract_horizons_are_accepted():
    """A non-contract horizon is a programming error, not a horizon."""
    horizons = _horizons()
    with pytest.raises(ValueError):
        horizons.required_exit_session("2026-09-04", 30)
    with pytest.raises(ValueError):
        horizons.select_exit_bar([{"session_date": "2026-09-08"}], 1)


def test_select_exit_bar_never_substitutes_a_later_bar():
    horizons = _horizons()
    bars = [{"session_date": f"2026-06-{day:02d}"} for day in range(1, 32)]
    assert horizons.select_exit_bar(bars, 21) is bars[20]
    # Immature: fewer sessions than the horizon -> no exit at all, never bars[-1].
    assert horizons.select_exit_bar(bars[:20], 21) is None
    assert horizons.select_exit_bar([], 21) is None
    # Only the contract horizons are horizon values at all.
    with pytest.raises(ValueError):
        horizons.select_exit_bar(bars, 30)


def test_horizons_are_the_four_contract_values():
    horizons = _horizons()
    assert horizons.HORIZON_SESSIONS == (21, 63, 126, 252)


def test_session_horizon_due_date_uses_the_next_session_as_entry():
    horizons = _horizons()
    """The written date is the exit session of the horizon, from as_of's next session."""
    as_of = "2026-08-28"  # Friday
    entry = next_session(date.fromisoformat(as_of))
    assert entry.isoformat() == "2026-08-31"
    assert horizons.session_horizon_due_date(as_of, 21) == (
        horizons.required_exit_session(entry.isoformat(), 21)
    )


def test_a_due_date_never_delays_a_genuinely_mature_horizon():
    horizons = _horizons()
    """The scheduling gate fires on or before the true maturity date."""
    for horizon in horizons.HORIZON_SESSIONS:
        for as_of in ("2026-01-15", "2026-05-29", "2026-08-28"):
            due = date.fromisoformat(horizons.session_horizon_due_date(as_of, horizon))
            entry = next_session(date.fromisoformat(as_of))
            # The exit session cannot fall before the date written for it.
            assert due >= entry
            assert count_sessions(entry, due, inclusive=False) == horizon


@pytest.mark.parametrize("horizon", (21, 63, 126, 252))  # the contract values
def test_legacy_due_dates_are_at_most_a_few_sessions_early(horizon):
    horizons = _horizons()
    """Existing immutable rows keep working as advisory scheduling hints."""
    as_of = "2026-08-28"
    legacy = date.fromisoformat(horizons.legacy_advisory_due_date(as_of, horizon))
    exact = date.fromisoformat(horizons.session_horizon_due_date(as_of, horizon))
    drift_sessions = abs(count_sessions(min(legacy, exact), max(legacy, exact)))
    assert drift_sessions <= 5, f"h{horizon}: {drift_sessions} sessions of scheduling drift"


# ---------------------------------------------------------------------------
# The definition is shared, not merely duplicated with equal values
# ---------------------------------------------------------------------------
ASSIGNMENT = re.compile(r"^HORIZON_SESSIONS\s*=\s*\(([^)]*)\)", re.MULTILINE)


def test_every_declared_horizon_tuple_matches_the_one_definition():
    """No module may declare its own horizon values that differ.

    Only horizons.py *defines* them; anything else that still declares the tuple
    (research-plane modules kept for a narrower diff) must agree exactly, so a
    future edit to one of them cannot silently fork the contract.
    """
    horizons = _horizons()
    declared = {}
    for path in _python_sources():
        if path.name == "horizons.py":
            continue
        for match in ASSIGNMENT.finditer(path.read_text(encoding="utf-8")):
            values = tuple(int(part.strip()) for part in match.group(1).split(",") if part.strip())
            declared[str(path.relative_to(REPO_ROOT))] = values
    assert declared, "expected at least one module to still declare the tuple"
    for module, values in declared.items():
        assert values == horizons.HORIZON_SESSIONS, f"{module} declares {values}"


def test_the_research_builder_uses_the_shared_exit_rule():
    """Identity, not equivalence: one function object is the rule."""
    horizons = _horizons()
    from tradehub_research.validation import outcome_builder

    assert outcome_builder.HORIZON_SESSIONS == horizons.HORIZON_SESSIONS
    assert outcome_builder.select_exit_bar is horizons.select_exit_bar


def test_the_research_builders_historical_rule_is_reproduced():
    """The shared rule is behaviour-identical to the pre-refactor expression."""
    horizons = _horizons()
    bars = [{"session_date": f"2026-06-{day:02d}"} for day in range(1, 31)]
    for horizon in (21, 63, 126, 252):
        historical = bars[horizon - 1] if len(bars) >= horizon else None
        assert horizons.select_exit_bar(bars, horizon) is historical


def test_the_forward_maturation_uses_the_shared_rule():
    """The forward path imports the exit rule rather than reimplementing it."""
    from tradehub_research.ops import outcome_maturation

    assert outcome_maturation.select_exit_bar is _horizons().select_exit_bar
    assert outcome_maturation.required_exit_session is _horizons().required_exit_session
    assert outcome_maturation.HORIZON_SESSIONS == _horizons().HORIZON_SESSIONS
