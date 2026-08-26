"""Execution-plane production seam for persisted Phase-4 proposals.

This module owns the preview capability and never exposes its credential or raw
confirmation token to the research plane.  Research receives only the safe link
row written to the research database.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from tradehub.client import TradeHubClient
from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.execution import PreviewIntent, proposal_to_preview_intent
from tradehub_research.universe import SecurityIdentityStore


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Phase4Runtime:
    """Load, revalidate, preview, and durably link one persisted proposal."""

    def __init__(
        self,
        database: ResearchDB,
        *,
        allowlist: set[str],
        max_day_count: int,
        max_day_notional: float,
        preview_client: TradeHubClient | None = None,
    ) -> None:
        self.database = database
        self.allowlist = {item.upper() for item in allowlist}
        self.max_day_count = max_day_count
        self.max_day_notional = max_day_notional
        self.preview_client = preview_client or TradeHubClient(preview_only=True)

    def _load_intent(self, proposal_id: str) -> PreviewIntent:
        with self.database.connect() as db:
            proposal = db.execute(
                "SELECT p.*, d.observed_at FROM trade_proposal p "
                "JOIN portfolio_state_observation d ON d.decision_id=p.decision_id "
                "WHERE p.proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise ValueError(f"persisted proposal not found: {proposal_id}")
            existing = db.execute(
                "SELECT state,execution_ref FROM phase4_execution_link WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if existing is not None and existing["state"] in {
                "PREVIEWED",
                "APPROVED",
                "SUBMITTED",
            }:
                raise ValueError(
                    f"proposal already has active execution: {existing['execution_ref']}"
                )
            day = proposal["activity_date"]
            usage = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(max_notional_microusd),0) "
                "FROM trade_proposal WHERE activity_date=?",
                (day,),
            ).fetchone()
            identity = SecurityIdentityStore.ticker_at_connection(
                db, str(proposal["security_id"]), str(proposal["created_at"])
            )
            values = dict(proposal)
            values["as_of"] = str(proposal["created_at"])
            return proposal_to_preview_intent(
                values,
                allowlist=self.allowlist,
                current_day_count=int(usage[0]),
                current_day_notional=float(usage[1]) / 1_000_000,
                max_day_count=self.max_day_count,
                max_day_notional=self.max_day_notional,
                identity_as_of=str(proposal["created_at"]),
                resolve_ticker=lambda _security_id, _as_of: identity,
            )

    async def preview_proposal(self, proposal_id: str) -> dict[str, Any]:
        intent = self._load_intent(proposal_id)
        result = await self.preview_client.post(
            "/orders/preview",
            {
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "order_type": intent.order_type,
                "limit_price": intent.limit_price,
                "currency": intent.currency,
                "reason": intent.reason,
                "client_request_id": intent.proposal_id,
            },
        )
        if result.get("accepted") is not True or not result.get("confirmation_token"):
            raise ValueError("broker preview was not accepted")
        token_ref = hashlib.sha256(str(result["confirmation_token"]).encode()).hexdigest()
        execution_ref = f"execution:{proposal_id}"
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO phase4_execution_link "
                "(proposal_id,execution_ref,state,approval_ref_hash,previewed_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(proposal_id) DO UPDATE SET execution_ref=excluded.execution_ref, "
                "state=excluded.state,approval_ref_hash=excluded.approval_ref_hash,"
                "previewed_at=excluded.previewed_at",
                (proposal_id, execution_ref, "PREVIEWED", token_ref, _now()),
            )
        return {
            "accepted": True,
            "proposal_id": proposal_id,
            "execution_ref": execution_ref,
            "intent": intent,
        }
