"""Backfill package: bounded live-fetch ingestion for Phase 5.

Honors the survivorship rule from the steering directive: with no historical
PIT universe in the database, a present-day 450-symbol sample is a
BOOTSTRAP_COHORT -- labeled as such, never promoted to a historical PIT
universe, and never used to claim broad-universe backtest performance.
"""

from __future__ import annotations
