#!/bin/bash
# TradeHub health watch: canonical deployed code + canonical research state.
#
# The watch now DIAGNOSES, REMEDIATES and VERIFIES market-data freshness, not
# just reports it, so it needs the provider credentials the systemd research
# units get from /etc/tradehub/research.env. Without them the fetches would fail
# closed on a missing token and the remediation would be a no-op.
#
# Alerts/reports are stdout; exit 0 is returned for any watch that RAN (the
# Hermes no_agent watchdog delivers non-empty stdout, and a non-zero exit is
# reserved for a genuine checker crash).
set -euo pipefail
exec runuser -u tradehub-research -- bash -c '
  set -a
  . /etc/tradehub/research.env
  set +a
  export TRADEHUB_RESEARCH_DIR=/var/lib/tradehub-research
  exec /opt/tiger-tradehub/.venv/bin/python -m tradehub_research.ops.health_watch
'
