"""Portfolio plane: state machine, policy, risk, sizing, proposals, briefing.

Research-only.  No execution imports, no credentials, no token handling.
The narrow public surface is: PortfolioEngine, PolicySpec/PolicyRegistry,
PortfolioSnapshot/SignalInput, and the fixture policy builder used by tests
and the RA-03 acceptance pack.
"""

from __future__ import annotations

from tradehub_research.portfolio.engine import PortfolioEngine, RunSummary
from tradehub_research.portfolio.policy import (
    PolicyRegistry,
    PolicySpec,
    build_policy,
    load_policy_from_json,
)
from tradehub_research.portfolio.snapshot import (
    PortfolioSnapshot,
    SignalInput,
    build_signal_input,
    build_snapshot,
)

__all__ = [
    "PortfolioEngine",
    "RunSummary",
    "PolicyRegistry",
    "PolicySpec",
    "build_policy",
    "load_policy_from_json",
    "PortfolioSnapshot",
    "SignalInput",
    "build_signal_input",
    "build_snapshot",
]
