"""Loopback-only authenticated HTTP surface for the Phase 2 committee."""

from __future__ import annotations

import hmac
from functools import lru_cache
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, status

from tradehub_research.committee.pack import EvidencePackBuilder
from tradehub_research.committee.routing import CommitteeRouter
from tradehub_research.committee.scoring import Scorer
from tradehub_research.committee.store import CommitteeStore
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB

app = FastAPI(title="TradeHub Research Committee", version="1")


@lru_cache
def get_settings() -> ResearchSettings:
    return ResearchSettings()


@lru_cache
def get_database() -> ResearchDB:
    database = ResearchDB(get_settings().db_path)
    database.migrate()
    return database


def require_auth(
    authorization: str | None = Header(default=None),
    settings: ResearchSettings = Depends(get_settings),
) -> None:
    parts = authorization.strip().split(None, 1) if authorization else []
    supplied = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None
    expected = settings.api_token.get_secret_value()
    if not expected or supplied is None or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing or invalid bearer token"
        )


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@app.post("/committee-runs", dependencies=[Depends(require_auth)])
def create_run(
    body: dict[str, Any], database: ResearchDB = Depends(get_database)
) -> dict[str, Any]:
    try:
        candidate_id = body["candidate_id"]
        pack = EvidencePackBuilder(database).build(candidate_id)
        store = CommitteeStore(database)
        comparator, scoring = store.ensure_registry_rows()
        run_id = store.create_or_resume_committee_run(
            candidate_id=candidate_id,
            pack_hash=pack.pack_hash,
            committee_policy_version=int(body.get("committee_policy_version", 1)),
            comparator_config_hash=body.get("comparator_config_hash", comparator),
            scoring_config_hash=body.get("scoring_config_hash", scoring),
            prompt_versions=body.get(
                "prompt_versions", {"neutral": "v1", "red_team": "v1", "arbiter": "v1"}
            ),
            assessment_schema_version=int(body.get("assessment_schema_version", 1)),
        )
        router = CommitteeRouter(database)
        router.initialize(run_id)
        return router.status(run_id)
    except (KeyError, ValueError) as exc:
        raise _http_error(exc) from exc


@app.get("/committee-runs/{run_id}/work", dependencies=[Depends(require_auth)])
def get_work(run_id: str, database: ResearchDB = Depends(get_database)) -> dict[str, Any]:
    try:
        work = CommitteeRouter(database).get_work(run_id)
        return {"work": work}
    except (KeyError, ValueError) as exc:
        raise _http_error(exc) from exc


@app.post("/committee-runs/{run_id}/assessments", dependencies=[Depends(require_auth)])
def submit_assessment(
    run_id: str, body: dict[str, Any], database: ResearchDB = Depends(get_database)
) -> dict[str, Any]:
    try:
        return CommitteeRouter(database).submit(run_id, body)
    except (KeyError, ValueError) as exc:
        raise _http_error(exc) from exc


@app.get("/committee-runs/{run_id}", dependencies=[Depends(require_auth)])
def get_run(run_id: str, database: ResearchDB = Depends(get_database)) -> dict[str, Any]:
    try:
        return CommitteeRouter(database).status(run_id)
    except (KeyError, ValueError) as exc:
        raise _http_error(exc) from exc


@app.get("/score-snapshots/{snapshot_id}", dependencies=[Depends(require_auth)])
def get_snapshot(snapshot_id: str, database: ResearchDB = Depends(get_database)) -> dict[str, Any]:
    try:
        return Scorer(database).get_snapshot(snapshot_id)
    except KeyError as exc:
        raise _http_error(exc) from exc


@app.get("/candidates/{candidate_id}/score-snapshots", dependencies=[Depends(require_auth)])
def list_snapshots(
    candidate_id: str, database: ResearchDB = Depends(get_database)
) -> dict[str, Any]:
    return {"score_snapshots": Scorer(database).list_candidate(candidate_id)}


def main() -> None:
    settings = get_settings()
    if settings.bind_host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("research committee must bind to loopback")
    uvicorn.run(
        "tradehub_research.committee.api:app", host=settings.bind_host, port=settings.bind_port
    )
