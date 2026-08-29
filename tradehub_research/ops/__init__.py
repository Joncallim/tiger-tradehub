"""TradeHub ops: deterministic daily operating jobs (issue #39).

Simple single-host schedule (systemd timers, no scheduler framework).
Every job is a deterministic CLI (``python -m tradehub_research.ops.X``)
that writes its own append-only/health artifacts; no LLM inside any job.
"""
