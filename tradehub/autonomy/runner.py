"""Deterministic autonomous PAPER runner (issue #51 C/J/K).

THE PRIVILEGED ACTOR IS THIS PROGRAM, NOT A MODEL. It:
- never calls an LLM, never consumes filings/news/text, has no prompt
  surface, never decides whether a stock is attractive, never changes
  investment weights or risk policy;
- receives ONLY typed eligible trade_proposal envelopes (research-side,
  portfolio-engine-produced; a proposal may carry `fixture: true` ONLY for
  the separately-marked deterministic acceptance fixture);
- revalidates: versioned PAPER policy, kill switch, positive PAPER account
  proof (via the execution API's live broker query), proposal freshness,
  data freshness, symbol allowlist, holdings/long-only, daily count/notional
  budgets, per-position exposure;
- invokes the EXISTING guarded execution path (preview -> submit with the
  confirmation token) with autonomous=True (the execution API additionally
  enforces the kill switch on autonomous submits);
- reconciles the broker outcome, settles the actual fill delta (the budget
  ledger charges once per proposal; fills are recorded, never re-ordered),
  and records a sanitized result to an append-only ledger.

Zero eligible proposals -> zero orders (a successful run).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from tradehub.autonomy import kill_switch
from tradehub.autonomy.budgets import BUDGET_DB, charge, daily_usage
from tradehub.autonomy.policy import POLICY_FILE, PaperAutonomyPolicy, load_policy
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB, utc_now

INBOX_DIR = Path("/var/lib/tradehub/autonomy/proposals")
PROCESSED_DIR = INBOX_DIR / "processed"
LEDGER_FILE = Path("/var/lib/tradehub-research/autonomy/paper_run_ledger.jsonl")

EXECUTION_API = __import__("os").getenv("TRADEHUB_EXECUTION_API", "http://127.0.0.1:8787")
AUTONOMOUS_TAG = "autonomous-paper-v1"


class AutonomyRefusal(Exception):
    """Deterministic refusal with a machine-readable reason."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client(settings: ResearchSettings):
    import os

    import httpx

    class _Client:
        def __init__(self) -> None:
            # The runner authenticates with the SCOPED autonomy token (never a
            # Tiger credential): TRADEHUB_AUTONOMY_TOKEN, falling back to the
            # research API token for isolated/local runs.
            self._token = os.getenv("TRADEHUB_AUTONOMY_TOKEN") or (
                settings.api_token.get_secret_value() if settings.api_token else ""
            )

        def _headers(self) -> dict[str, str]:
            return {"Authorization": f"Bearer {self._token}"}

        def get(self, path: str) -> dict:
            with httpx.Client(timeout=30) as http:
                response = http.get(f"{EXECUTION_API}{path}", headers=self._headers())
                response.raise_for_status()
                return response.json()

        def post(self, path: str, payload: dict) -> dict:
            with httpx.Client(timeout=60) as http:
                response = http.post(
                    f"{EXECUTION_API}{path}", headers=self._headers(), json=payload
                )
                response.raise_for_status()
                return response.json()

    return _Client()


def prove_paper_account(client, settings: ResearchSettings) -> dict:
    """Positively establish the broker account is PAPER (issue #51 F).

    The proof is a LIVE broker query performed by the execution service
    (which holds the credentials): the BROKER's own account_type must be
    PAPER (Tiger's paper accounts live on the production API since the
    sandbox was deprecated), OR the environment must be the legacy
    PAPER_SANDBOX; plus a live managed-account + assets round-trip must
    succeed. Never inferred from account-number format, config prose, or a
    cached artifact. Failure -> NO TRADE.
    """
    proof = client.get("/account/proof")
    broker_paper = proof.get("account_type") == "PAPER"
    sandbox = proof.get("environment") == "PAPER_SANDBOX"
    if not (broker_paper or sandbox):
        raise AutonomyRefusal(
            f"PAPER proof failed: broker account_type={proof.get('account_type')!r} "
            f"environment={proof.get('environment')!r} (not a paper account); "
            "refusing any autonomous write"
        )
    if not proof.get("assets_ok"):
        raise AutonomyRefusal("PAPER proof failed: live assets round-trip did not succeed")
    if proof.get("account_status") not in ("Open", "Funded", "New"):
        raise AutonomyRefusal(f"PAPER proof failed: account status {proof.get('account_status')!r}")
    return proof


def _resolve_ticker(research_db: ResearchDB):
    def resolve(security_id: str, as_of: str) -> str | None:
        with research_db.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT canonical_ticker FROM security WHERE security_id=?", (security_id,)
            ).fetchone()
        return str(row["canonical_ticker"]).upper() if row and row["canonical_ticker"] else None

    return resolve


def _proposal_age_ok(proposal: dict, policy: PaperAutonomyPolicy, now: datetime) -> bool:
    created = proposal.get("created_at") or proposal.get("exported_at")
    if not created:
        return False
    try:
        age = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return False
    return age <= policy.proposal_max_age_seconds


def _data_age_ok(envelope: dict, policy: PaperAutonomyPolicy, now: datetime) -> bool:
    data_as_of = envelope.get("data_as_of")
    if not data_as_of:
        return False
    try:
        age = (now.date() - date.fromisoformat(str(data_as_of)[:10])).days * 86400
    except ValueError:
        return False
    return age <= policy.data_max_age_seconds


def _validate_envelope(envelope: dict, policy: PaperAutonomyPolicy, now: datetime) -> None:
    proposal = envelope.get("proposal")
    if not isinstance(proposal, dict):
        raise AutonomyRefusal("envelope has no typed proposal")
    if "proposal_id" not in proposal:
        raise AutonomyRefusal("proposal missing proposal_id (typed trade-proposal required)")
    if envelope.get("fixture") and not str(envelope.get("fixture_tag", "")).startswith(
        "paper-acceptance-fixture"
    ):
        raise AutonomyRefusal("fixture proposals require a marked acceptance-fixture tag")
    if not _proposal_age_ok(proposal, policy, now):
        raise AutonomyRefusal("proposal is stale (older than proposal_max_age)")
    if not _data_age_ok(envelope, policy, now):
        raise AutonomyRefusal("market data is stale (older than data_max_age)")
    if str(envelope.get("universe", "")).upper() not in {
        u.upper() for u in policy.allowed_universe
    }:
        raise AutonomyRefusal(
            f"proposal universe {envelope.get('universe')!r} not in allowed_universe"
        )


def _validate_exposure(proposal: dict, policy: PaperAutonomyPolicy) -> None:
    target_weight_ppm = proposal.get("target_weight_ppm")
    if target_weight_ppm is None:
        raise AutonomyRefusal("proposal missing target_weight_ppm (exposure bound required)")
    if int(target_weight_ppm) > policy.max_per_position_exposure_ppm:
        raise AutonomyRefusal(
            f"per-position exposure {target_weight_ppm} ppm exceeds policy max "
            f"{policy.max_per_position_exposure_ppm} ppm"
        )


def _order_payload(proposal: dict, symbol: str, policy: PaperAutonomyPolicy) -> dict:
    """Map the typed proposal to the EXISTING OrderIntent contract (no new
    order route; long-only BUY for ENTER/ADD, SELL bounded by holdings)."""
    action = str(proposal["action"]).upper()
    if action not in ("BUY", "SELL"):
        raise AutonomyRefusal(f"unsupported action {action!r} (long-only US equities only)")
    quantity_microunits = int(
        proposal.get("completion_quantity_microunits")
        or proposal.get("max_quantity_microunits")
        or 0
    )
    if quantity_microunits <= 0:
        raise AutonomyRefusal("proposal has zero executable quantity")
    if quantity_microunits % 1_000_000 != 0:
        raise AutonomyRefusal(
            "fractional share quantities are not supported (US stocks, whole shares only)"
        )
    if quantity_microunits < 1_000_000:
        raise AutonomyRefusal("quantity below one whole share")
    if action == "SELL":
        current = proposal.get("current_quantity_microunits")
        sellable = proposal.get("sellable_quantity_microunits")
        if sellable is not None and quantity_microunits > int(sellable):
            raise AutonomyRefusal("SELL quantity exceeds sellable holdings (long-only)")
        if current is not None and quantity_microunits > int(current):
            raise AutonomyRefusal("SELL quantity exceeds current holdings (long-only)")
    mark = proposal.get("mark_price_microusd")
    if not mark:
        raise AutonomyRefusal("proposal missing mark_price_microusd (LIMIT price required)")
    return {
        "symbol": symbol,
        "side": action,
        "quantity": quantity_microunits / 1_000_000,
        "order_type": "LIMIT",
        "limit_price": int(mark) / 1_000_000,
        "currency": "USD",
        "reason": f"autonomous-paper-v1:{proposal.get('proposal_id', '')[:24]}",
        "client_request_id": f"ap-{proposal.get('proposal_id', '')[:40]}",
    }


def _record_ledger(entry: dict, ledger_path: Path = LEDGER_FILE) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def run_autonomy(
    *,
    settings: ResearchSettings,
    policy_path: Path = POLICY_FILE,
    inbox: Path = INBOX_DIR,
    ledger: Path = LEDGER_FILE,
    budget_db: Path = BUDGET_DB,
    allowlist: set[str] | None = None,
    api_client=None,
    now: datetime | None = None,
    paper_proof: dict | None = None,
    kill_switch_path: Path | None = None,
) -> dict:
    """One deterministic autonomous-PAPER run. Returns the run summary.

    Deterministic + injectable for tests (now/api_client/paper_proof/allowlist).
    """
    now = now or _now()
    policy = load_policy(policy_path)
    if not policy.enabled:
        return {"status": "DISABLED", "reason": "autonomy policy disabled", "orders": 0}
    kill_path = kill_switch_path or kill_switch.KILL_SWITCH_FILE
    if policy.kill_switch or kill_switch.is_blocked(kill_path):
        return {"status": "BLOCKED", "reason": "kill switch engaged", "orders": 0}
    kill_switch.assert_allowed(kill_path)

    client = api_client or _client(settings)
    try:
        proof = paper_proof or prove_paper_account(client, settings)
    except AutonomyRefusal as exc:
        return {
            "status": "REFUSED",
            "reason": str(exc),
            "orders": 0,
            "proposals_seen": 0,
            "refusals": [],
            "executions": [],
        }
    allowlist = allowlist or set(client.get("/config/allowlist").get("symbols", []))

    inbox.mkdir(parents=True, exist_ok=True)
    processed_dir = inbox / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    envelope_files = sorted(inbox.glob("*.json"))
    day = now.date().isoformat()

    summary = {
        "status": "OK",
        "policy_version": policy.policy_version,
        "orders": 0,
        "proposals_seen": 0,
        "refusals": [],
        "executions": [],
        "at": utc_now(),
        "paper_proof": {
            k: proof.get(k) for k in ("environment", "account", "account_status", "assets_ok")
        },
    }

    research_db = ResearchDB(settings.db_path, settings.busy_timeout_ms)
    resolve = _resolve_ticker(research_db)

    for path in envelope_files:
        try:
            envelope = json.loads(path.read_text())
        except (ValueError, OSError) as exc:
            summary["refusals"].append(
                {"proposal_id": path.stem, "reason": f"unparseable envelope: {exc}"}
            )
            continue
        summary["proposals_seen"] += 1
        proposal = envelope.get("proposal", {})
        proposal_id = proposal.get("proposal_id", path.stem)
        try:
            _validate_envelope(envelope, policy, now)
            _validate_exposure(proposal, policy)
            symbol = str(envelope.get("symbol") or "").upper()
            if not symbol:
                raise AutonomyRefusal("envelope missing authoritative symbol")
            # Cross-check against the research identity when resolvable: a
            # mismatch between the exported symbol and the PIT identity is a
            # refusal (the DB may be absent in isolated acceptance runs).
            db_symbol = resolve(
                str(proposal.get("security_id", "")), str(envelope.get("data_as_of", ""))
            )
            if db_symbol and db_symbol != symbol:
                raise AutonomyRefusal(
                    f"symbol mismatch: envelope {symbol!r} vs identity {db_symbol!r}"
                )
            if symbol not in {s.upper() for s in allowlist}:
                raise AutonomyRefusal(f"symbol {symbol!r} not in the execution allowlist")
            notional_microusd = int(proposal.get("max_notional_microusd") or 0)
            if notional_microusd <= 0:
                raise AutonomyRefusal("proposal missing positive max_notional_microusd")
            usage = daily_usage(day, path=budget_db)
            if usage["count"] >= policy.max_order_count_per_day:
                raise AutonomyRefusal("daily order-count budget exhausted")
            if (
                usage["notional_microusd"] + notional_microusd
                > policy.max_notional_per_day_microusd
            ):
                raise AutonomyRefusal("daily notional budget exhausted")

            # The proposal is fully validated: charge the budget ONCE
            # (idempotent per proposal) and execute via the existing path.
            if not charge(day, proposal_id, notional_microusd, path=budget_db):
                summary["refusals"].append(
                    {"proposal_id": proposal_id, "reason": "already charged (duplicate)"}
                )
                continue

            payload = _order_payload(proposal, symbol, policy)
            preview = client.post("/orders/preview", payload)
            token = preview.get("confirmation_token")
            if not token:
                raise AutonomyRefusal(
                    f"execution preview rejected: {preview.get('policy_warnings', preview)}"
                )
            # Kill switch re-check immediately before the write.
            if policy.kill_switch or kill_switch.is_blocked(kill_path):
                raise AutonomyRefusal("kill switch engaged between preview and submit")
            try:
                submit = client.post(
                    "/orders/submit",
                    {
                        "confirmation_token": token,
                        "autonomous": True,
                        "autonomy_tag": AUTONOMOUS_TAG,
                    },
                )
                submit_error = None
            except Exception as exc:  # noqa: BLE001 -- indeterminate submit: outcome unknown
                submit, submit_error = {}, f"{type(exc).__name__}: {exc}"
            if submit.get("dry_run"):
                # The guarded path never touched the broker: there is no order
                # to reconcile. Record the dry-run outcome honestly.
                reconciliation = {"status": "DRY_RUN_NO_ORDER"}
                reconcile_error = None
            else:
                try:
                    reconciliation = client.post(
                        "/orders/submit/reconcile", {"confirmation_token": token}
                    )
                    reconcile_error = None
                except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
                    reconciliation, reconcile_error = {}, f"{type(exc).__name__}: {exc}"
            entry = {
                "proposal_id": proposal_id,
                "symbol": symbol,
                "action": str(proposal.get("action", "")),
                "decision": "EXECUTED" if not submit_error else "INDETERMINATE",
                "dry_run": bool(submit.get("dry_run")),
                "submitted": bool(submit.get("submitted")),
                "order_id": submit.get("order_id"),
                "reconcile_status": reconciliation.get("status"),
                "submit_error": submit_error,
                "reconcile_error": reconcile_error,
                "at": utc_now(),
            }
            _record_ledger(entry, ledger_path=ledger)
            summary["executions"].append(entry)
            summary["orders"] += 1
            path.rename(processed_dir / path.name)
        except (AutonomyRefusal, ValueError, RuntimeError, PermissionError) as exc:
            summary["refusals"].append({"proposal_id": proposal_id, "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 -- never let one proposal kill the run
            summary["refusals"].append(
                {"proposal_id": proposal_id, "reason": f"unexpected: {type(exc).__name__}: {exc}"}
            )

    return summary


def main(argv: list[str] | None = None) -> int:
    settings = ResearchSettings()
    summary = run_autonomy(settings=settings)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
