"""Execution-plane production seam for persisted Phase-4 proposals.

This module owns the preview/submit/reconcile capabilities and never exposes
its credential or raw confirmation token to the research plane. Research
receives only the safe link row written to the research database; the
execution-side ``AuditStore`` confirmation record (keyed by
``client_request_id = proposal_id``) is the sole holder of the raw
confirmation token, and is how ``Phase4ExecutionBoundary`` authority is
recovered after a process restart.

``Phase4ExecutionBoundary``'s preview/submit/reconcile callbacks are
synchronous by contract (see ``tradehub/phase4_execution.py``); this module
bridges them to the async ``TradeHubClient`` HTTP calls via
``_run_coro_blocking``, a dedicated-thread event loop that works whether or
not an outer event loop is already running.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, TypeVar

from tradehub.audit import AuditStore
from tradehub.client import TradeHubClient
from tradehub.phase4_execution import ApprovalContext, Phase4ExecutionBoundary
from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.execution import PreviewIntent, proposal_to_preview_intent
from tradehub_research.universe import SecurityIdentityStore

# Execution-link states surviving a restart; these block a second preview of
# the same proposal (single-proposal lifecycle, see Phase4ExecutionBoundary).
ACTIVE_STATES = {"PREVIEWED", "APPROVED", "SUBMITTED", "PARTIALLY_FILLED"}

_T = TypeVar("_T")


def _run_coro_blocking(factory: Any) -> _T:
    """Run an async coroutine factory to completion from SYNC code.

    Uses a dedicated worker thread with its own event loop so this works
    whether or not an outer event loop is already running (the boundary's
    sync callback contract must never be broken by making it async).
    """
    import asyncio

    def _runner() -> _T:
        return asyncio.run(factory())

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Phase4Runtime:
    """Load, revalidate, preview, approve, submit, and reconcile one proposal.

    This is the smallest agent-invokable production entrypoint: it composes
    the existing guarded ``/orders/preview`` HTTP capability, the existing
    ``Phase4ExecutionBoundary`` (approval binding, single-use lifecycle,
    settlement direction), and the existing execution-side ``AuditStore``
    (raw-token authority, restart recovery) -- no new broker-write service is
    created.
    """

    def __init__(
        self,
        database: ResearchDB,
        *,
        allowlist: set[str],
        max_day_count: int,
        max_day_notional: float,
        preview_client: TradeHubClient | None = None,
        submit_client: TradeHubClient | None = None,
        audit_store: AuditStore | None = None,
        prove_paper: Any = None,
    ) -> None:
        self.database = database
        self.allowlist = {item.upper() for item in allowlist}
        self.max_day_count = max_day_count
        self.max_day_notional = max_day_notional
        self.preview_client = preview_client or TradeHubClient(preview_only=True)
        self.submit_client = submit_client or TradeHubClient()
        self.audit_store = audit_store
        self._prove_paper = prove_paper or (lambda: True)

    # -- persisted-proposal loading & revalidation -------------------------

    def _load_proposal_row(self, db: Any, proposal_id: str) -> Any:
        proposal = db.execute(
            "SELECT p.*, d.observed_at FROM trade_proposal p "
            "JOIN portfolio_state_observation d ON d.decision_id=p.decision_id "
            "WHERE p.proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if proposal is None:
            raise ValueError(f"persisted proposal not found: {proposal_id}")
        return proposal

    def _load_link(self, db: Any, proposal_id: str) -> Any:
        return db.execute(
            "SELECT * FROM phase4_execution_link WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()

    def _load_intent(self, db: Any, proposal: Any) -> PreviewIntent:
        day = proposal["activity_date"]
        usage = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(max_notional_microusd),0) "
            "FROM trade_proposal WHERE activity_date=? AND proposal_id!=?",
            (day, proposal["proposal_id"]),
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

    def _current_quantity(self, db: Any, proposal: Any, link: Any = None) -> float:
        # The execution link's running owned_quantity_microunits (updated on
        # every reconciliation by _persist_link_callback) is authoritative
        # once any fill has been applied to THIS proposal's execution -- the
        # static portfolio_holding snapshot is a decision-time snapshot and
        # does not reflect fills from prior reconciliations of this same
        # proposal within its own lifecycle.
        if link is not None and link["owned_quantity_microunits"] is not None:
            return float(link["owned_quantity_microunits"]) / 1_000_000
        row = db.execute(
            "SELECT quantity_microunits FROM portfolio_holding "
            "WHERE snapshot_id=? AND security_id=?",
            (proposal["portfolio_snapshot_id"], proposal["security_id"]),
        ).fetchone()
        return float(row["quantity_microunits"]) / 1_000_000 if row is not None else 0.0

    # -- Stage 1: preview ---------------------------------------------------

    async def preview_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self.database.connect() as db:
            proposal = self._load_proposal_row(db, proposal_id)
            existing = self._load_link(db, proposal_id)
            if existing is not None and existing["state"] in ACTIVE_STATES:
                raise ValueError(
                    f"proposal already has active execution: {existing['execution_ref']}"
                )
            intent = self._load_intent(db, proposal)

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

    # -- persistence callback for the boundary ------------------------------

    def _persist_link_callback(self, proposal_id: str):
        def _persist(_proposal_id: str, execution_ref: str, metadata: dict[str, str]) -> None:
            fields = []
            values: list[Any] = []
            if "confirmation_token_ref" in metadata:
                fields.append("approval_ref_hash=?")
                values.append(metadata["confirmation_token_ref"])
            if "broker_order_ref" in metadata:
                fields.extend(["broker_order_ref=?", "submitted_at=?"])
                values.extend([metadata["broker_order_ref"], _now()])
            if "settlement_state" in metadata:
                fields.extend(["state=?", "reconciled_at=?"])
                values.extend([metadata["settlement_state"], _now()])
            if "applied_fill" in metadata:
                microunits = int(round(float(metadata["applied_fill"]) * 1_000_000))
                fields.append("applied_fill_microunits=?")
                values.append(microunits)
            if "owned_quantity" in metadata:
                owned_microunits = int(round(float(metadata["owned_quantity"]) * 1_000_000))
                fields.append("owned_quantity_microunits=?")
                values.append(owned_microunits)
            if not fields:
                return
            values.append(proposal_id)
            with self.database.connect() as db:
                db.execute(
                    f"UPDATE phase4_execution_link SET {','.join(fields)} WHERE proposal_id=?",
                    tuple(values),
                )

        return _persist

    # -- restart-safe boundary recovery --------------------------------------

    def _recover_boundary(
        self, proposal_id: str, intent: PreviewIntent, link: Any
    ) -> Phase4ExecutionBoundary:
        """Reconstruct a boundary in the PREVIEWED/APPROVED/SUBMITTED state
        from execution-side authority, without re-invoking preview.

        The raw confirmation token never crosses into research state: it is
        looked up here, execution-side only, from ``AuditStore`` by
        ``client_request_id == proposal_id``, and its SHA-256 is verified
        against the safe research-side ``approval_ref_hash`` before use.
        """
        if self.audit_store is None:
            raise ValueError("execution-side AuditStore is required to recover authority")
        recovered = self.audit_store.find_confirmation_by_client_request_id(proposal_id)
        if recovered is None:
            raise ValueError(
                f"no active execution-side confirmation for proposal {proposal_id}; "
                "authority cannot be recovered"
            )
        token, _recovered_intent, _submission_state = recovered
        token_ref = hashlib.sha256(token.encode()).hexdigest()
        if token_ref != link["approval_ref_hash"]:
            raise ValueError(
                f"confirmation hash mismatch for proposal {proposal_id}; "
                "refusing to recover mismatched authority"
            )
        already_applied_fill = float(link["applied_fill_microunits"] or 0) / 1_000_000

        def submit(_token: str) -> str:
            async def _call() -> dict[str, Any]:
                return await self.submit_client.post(
                    "/orders/submit", {"confirmation_token": _token}
                )

            response = _run_coro_blocking(_call)
            order_id = response.get("order_id")
            if not order_id:
                raise ValueError("submit did not return a broker order id")
            return str(order_id)

        def reconcile(_broker_order_ref: str) -> dict[str, Any] | None:
            async def _call() -> dict[str, Any]:
                return await self.submit_client.get("/account/orders", {"limit": 100})

            response = _run_coro_blocking(_call)
            for order in response.get("orders", []):
                if str(order.get("id")) == str(_broker_order_ref) or str(
                    order.get("order_id")
                ) == str(_broker_order_ref):
                    return order
            return None

        return Phase4ExecutionBoundary.recover_previewed(
            intent=intent,
            confirmation_token=token,
            execution_ref=link["execution_ref"],
            submit=submit,
            reconcile=reconcile,
            prove_paper=self._prove_paper,
            persist_execution_link=self._persist_link_callback(proposal_id),
            broker_order_ref=link["broker_order_ref"],
            already_applied_fill=already_applied_fill,
        )

    def _deterministic_rationale(self, proposal: Any) -> str:
        """Concise deterministic reason derived from the persisted proposal's
        own reason codes -- never caller-supplied free text. This closes the
        gap where a caller-chosen ``rationale`` string could otherwise be
        echoed back as its own "canonical" value (self-comparison is not a
        binding); the approval's reason is always this DB-derived string."""
        import json as _json

        codes = _json.loads(proposal["reason_codes_json"])
        return ", ".join(str(c) for c in codes) if codes else "no reason codes recorded"

    # -- Stage 2: render + affirm approval -----------------------------------

    async def render_approval(self, proposal_id: str) -> dict[str, Any]:
        """Render the exact approval from the persisted proposal/preview.

        Returns the rendered context for display; the caller must then call
        ``affirm_approval`` with that EXACT context (round-tripped, never
        reconstructed from prose) to actually submit. The rationale is
        derived deterministically from the proposal's own reason codes --
        it is never accepted as caller input, so it cannot be spoofed.
        """
        with self.database.connect() as db:
            proposal = self._load_proposal_row(db, proposal_id)
            link = self._load_link(db, proposal_id)
            if link is None or link["state"] != "PREVIEWED":
                raise ValueError(f"proposal {proposal_id} is not in PREVIEWED state")
            intent = self._load_intent(db, proposal)
            rationale = self._deterministic_rationale(proposal)

        boundary = self._recover_boundary(proposal_id, intent, link)
        context = boundary.render_approval(
            intent,
            current_state=str(proposal["current_state"]),
            proposed_state=str(proposal["proposed_state"]),
            rationale=rationale,
        )
        return {
            "proposal_id": context.proposal_id,
            "symbol": context.symbol,
            "side": context.side,
            "quantity": context.quantity,
            "order_type": context.order_type,
            "limit_price": context.limit_price,
            "currency": context.currency,
            "current_state": context.current_state,
            "proposed_state": context.proposed_state,
            "rationale": context.rationale,
            "score_snapshot_id": context.score_snapshot_id,
        }

    async def affirm_approval(
        self, proposal_id: str, *, exact_context: dict[str, Any]
    ) -> dict[str, Any]:
        """Explicit affirmation of the EXACT rendered context.

        Re-renders the canonical context from persisted state (the boundary
        instance from ``render_approval`` does not survive across an async
        MCP tool call boundary) and compares the caller's round-tripped
        context against it -- a fabricated/altered context can never match.
        The rationale used for comparison is ALWAYS the deterministic,
        DB-derived value -- a caller cannot supply their own rationale and
        have it treated as canonical.
        """
        with self.database.connect() as db:
            proposal = self._load_proposal_row(db, proposal_id)
            link = self._load_link(db, proposal_id)
            if link is None or link["state"] != "PREVIEWED":
                raise ValueError(f"proposal {proposal_id} is not in PREVIEWED state")
            intent = self._load_intent(db, proposal)
            rationale = self._deterministic_rationale(proposal)

        boundary = self._recover_boundary(proposal_id, intent, link)
        canonical = boundary.render_approval(
            intent,
            current_state=str(proposal["current_state"]),
            proposed_state=str(proposal["proposed_state"]),
            rationale=rationale,
        )
        caller_context = ApprovalContext(
            proposal_id=str(exact_context["proposal_id"]),
            symbol=str(exact_context["symbol"]),
            side=str(exact_context["side"]),
            quantity=float(exact_context["quantity"]),
            order_type=str(exact_context["order_type"]),
            limit_price=float(exact_context["limit_price"]),
            currency=str(exact_context["currency"]),
            current_state=str(exact_context["current_state"]),
            proposed_state=str(exact_context["proposed_state"]),
            rationale=str(exact_context.get("rationale", "")),
            score_snapshot_id=canonical.score_snapshot_id,
        )
        boundary.affirm(caller_context)
        with self.database.connect() as db:
            db.execute(
                "UPDATE phase4_execution_link SET state='SUBMITTED',approved_at=? "
                "WHERE proposal_id=?",
                (_now(), proposal_id),
            )
        return {"proposal_id": proposal_id, "state": "SUBMITTED"}

    # -- Stage 3: reconcile + settle ------------------------------------------

    async def reconcile_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self.database.connect() as db:
            proposal = self._load_proposal_row(db, proposal_id)
            link = self._load_link(db, proposal_id)
            if link is None or link["state"] not in (
                {"SUBMITTED", "FILLED", "PARTIALLY_FILLED"} | ACTIVE_STATES
            ):
                raise ValueError(f"proposal {proposal_id} has no submitted execution to reconcile")
            intent = self._load_intent(db, proposal)
            current_quantity = self._current_quantity(db, proposal, link)

        boundary = self._recover_boundary(proposal_id, intent, link)
        # A recovered boundary's rendered/affirmed state does not persist
        # across process/call boundaries, so reconcile-only recovery
        # re-derives the SAME canonical render (never caller input) purely
        # to satisfy the boundary's own render->affirm->reconcile ordering
        # invariant -- no fresh broker submission occurs here, only
        # reconciliation of the ALREADY-submitted broker order.
        boundary.render_approval(
            intent,
            current_state=str(proposal["current_state"]),
            proposed_state=str(proposal["proposed_state"]),
            rationale="restart-recovered reconciliation",
        )
        result = boundary.reconcile_and_settle(current_quantity=current_quantity)
        return {
            "proposal_id": proposal_id,
            "settlement_state": result.settlement.state.value,
            "filled_qty": result.settlement.filled_qty,
            "owned_quantity": result.portfolio.owned_quantity,
            "sold_quantity": result.portfolio.sold_quantity,
            "next_state": result.portfolio.next_state,
            "portfolio_mutated": result.portfolio.portfolio_mutated,
        }
