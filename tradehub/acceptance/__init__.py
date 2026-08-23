"""Functional acceptance runner for Tiger TradeHub.

Packs are declarative, deterministic assertions grouped by pack ID.
The runner owns status classification, retries, timeouts, secret
sanitisation, run IDs, and commit capture. The agent only dispatches
packs by ID and reports the structured result.
"""

from tradehub.acceptance.runner import Status, main, run_pack

__all__ = ["Status", "main", "run_pack"]
