"""Register the six shipped Phase-1 Hunter implementations.

Importing this module populates ``SCREEN_REGISTRY`` in a deterministic order.
The registry is the single source of truth for the orchestration manifest.
"""

from __future__ import annotations

from tradehub_research.hunters import (
    event,
    inflection,
    informed_activity,
    momentum,
    quality,
    valuation,
)
from tradehub_research.screens import register_screen

_MODULES = (valuation, inflection, quality, informed_activity, event, momentum)

for _module in _MODULES:
    register_screen(_module.SCREEN_SPEC, _module.evaluate)
