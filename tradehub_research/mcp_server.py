"""Minimal, research-only MCP surface for committee workers."""

from __future__ import annotations

import json
from typing import Any

from tradehub_research.committee.routing import CommitteeRouter
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB


def create_server(database: ResearchDB | None = None) -> Any:
    """Create the three-tool server; dependency injection keeps discovery testable."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Install MCP support with: pip install -e '.[mcp]'") from exc

    if database is None:
        settings = ResearchSettings()
        database = ResearchDB(settings.db_path, settings.busy_timeout_ms)
        database.migrate()
    mcp = FastMCP("tiger-tradehub-research")

    @mcp.tool()
    def get_evidence_pack(candidate_id: str) -> dict[str, Any]:
        """Return the immutable research evidence pack for a candidate."""
        with database.connect(read_only=True) as db:
            row = db.execute(
                "SELECT pack_hash,body_json FROM evidence_pack "
                "WHERE candidate_id=? ORDER BY pack_spec_version DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"no evidence pack for candidate: {candidate_id}")
        return {"pack_hash": row["pack_hash"], "body": json.loads(row["body_json"])}

    @mcp.tool()
    def submit_assessment(
        committee_run_id: str, attempt_envelope: dict[str, Any]
    ) -> dict[str, Any]:
        """Record one issued committee work attempt and return current state."""
        return CommitteeRouter(database).submit(committee_run_id, attempt_envelope)

    @mcp.tool()
    def committee_status(committee_run_id: str) -> dict[str, Any]:
        """Return state plus each server-authorized current work envelope."""
        return CommitteeRouter(database).status(committee_run_id)

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
