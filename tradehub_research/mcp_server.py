"""Minimal, research-only MCP surface for committee workers."""

from __future__ import annotations

import json
from typing import Any

from tradehub_research.committee.routing import CommitteeRouter
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB


def resolve_evidence_artifact(
    database: ResearchDB, candidate_id: str, pack_hash: str | None = None
) -> dict[str, Any]:
    """Resolve one committee evidence artifact, by pin or as a debug convenience.

    Pinned lookups (``pack_hash`` given) return exactly that frozen artifact for
    that candidate and fail closed on any mismatch, so committee work can never
    be shown a different artifact than the one it was issued against.  Unpinned
    lookups exist for humans/debugging and are refused outright while the
    candidate has outstanding committee work.
    """
    with database.connect(read_only=True) as db:
        if pack_hash:
            row = db.execute(
                "SELECT pack_hash,pack_spec_version,body_json FROM evidence_pack "
                "WHERE candidate_id=? AND pack_hash=?",
                (candidate_id, pack_hash),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"candidate {candidate_id} has no artifact pinned at pack_hash {pack_hash}"
                )
        else:
            outstanding = db.execute(
                "SELECT count(*) FROM committee_run c "
                "JOIN committee_work w ON w.committee_run_id=c.committee_run_id "
                "LEFT JOIN model_call_attempt a ON a.work_id=w.work_id "
                "WHERE c.candidate_id=? AND a.attempt_id IS NULL",
                (candidate_id,),
            ).fetchone()[0]
            if outstanding:
                raise ValueError(
                    "unpinned evidence lookup refused: this candidate has outstanding "
                    "committee work; pass the pack_hash from the work envelope"
                )
            row = db.execute(
                "SELECT pack_hash,pack_spec_version,body_json FROM evidence_pack "
                "WHERE candidate_id=? ORDER BY pack_spec_version DESC, pack_hash LIMIT 1",
                (candidate_id,),
            ).fetchone()
    if row is None:
        raise ValueError(f"no evidence pack for candidate: {candidate_id}")
    body = json.loads(row["body_json"])
    lineage = body.get("lineage") if isinstance(body.get("lineage"), dict) else {}
    return {
        "pack_hash": row["pack_hash"],
        "pack_spec_version": row["pack_spec_version"],
        "representation": body.get("representation", "LEGACY_SCORING_PACK"),
        "lineage_hash": lineage.get("lineage_hash"),
        "pinned": bool(pack_hash),
        "body": body,
    }


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
    def get_evidence_pack(candidate_id: str, pack_hash: str | None = None) -> dict[str, Any]:
        """Return the committee evidence artifact for a candidate.

        Committee work is pinned: pass the ``pack_hash`` carried by the work
        envelope so the artifact a model is shown is exactly the artifact the
        run was issued against.  An unpinned lookup is a human/debug convenience
        and is refused outright while the candidate has outstanding committee
        work, so a worker can never silently retrieve a newer artifact than the
        one its work was issued for.
        """
        return resolve_evidence_artifact(database, candidate_id, pack_hash)

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
