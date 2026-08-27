import asyncio
from contextlib import contextmanager

from tradehub.phase4_runtime import Phase4Runtime


class Row(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, proposal):
        self.proposal = proposal
        self.inserts = []

    def execute(self, sql, params=()):
        if "SELECT p.*, d.observed_at" in sql:
            return Result(self.proposal)
        if "SELECT * FROM phase4_execution_link" in sql:
            return Result(None)
        if "SELECT COUNT(*), COALESCE" in sql:
            return Result((0, 0))
        if sql.startswith("INSERT INTO phase4_execution_link"):
            self.inserts.append((sql, params))
            return Result(None)
        raise AssertionError(f"unexpected SQL: {sql}")


class Database:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self, **kwargs):
        yield self.connection


class PreviewClient:
    def __init__(self):
        self.calls = []

    async def post(self, path, payload):
        self.calls.append((path, payload))
        return {"accepted": True, "confirmation_token": "execution-secret"}


class SubmitClient:
    def __init__(self):
        self.calls = []

    async def post(self, path, payload):
        self.calls.append((path, payload))
        return {"accepted": True}


def test_production_preview_loads_proposal_resolves_identity_and_persists_safe_link(monkeypatch):
    proposal = Row(
        proposal_id="p1",
        decision_id="d1",
        security_id="stable-security-id",
        activity_date="2025-01-01",
        created_at="2025-01-01T00:00:00Z",
        action="BUY",
        max_quantity_microunits=1_000_000,
        max_notional_microusd=150_000_000,
        score_snapshot_id="score1",
        portfolio_snapshot_id="portfolio1",
        policy_version="policy1",
        observed_at="2025-01-01T00:00:00Z",
    )
    connection = Connection(proposal)
    client = PreviewClient()
    monkeypatch.setattr(
        "tradehub.phase4_runtime.SecurityIdentityStore.ticker_at_connection",
        lambda db, security_id, as_of: "AAPL",
    )
    runtime = Phase4Runtime(
        Database(connection),
        allowlist={"AAPL"},
        max_day_count=3,
        max_day_notional=1000,
        preview_client=client,
        submit_client=SubmitClient(),
    )

    result = asyncio.run(runtime.preview_proposal("p1"))

    assert result["execution_ref"] == "execution:p1"
    assert client.calls[0][0] == "/orders/preview"
    assert client.calls[0][1]["symbol"] == "AAPL"
    assert client.calls[0][1]["client_request_id"] == "p1"
    assert len(connection.inserts) == 1
    params = connection.inserts[0][1]
    assert params[0:3] == ("p1", "execution:p1", "PREVIEWED")
    assert params[3] != "execution-secret"
    assert len(params[3]) == 64
