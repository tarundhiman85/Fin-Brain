"""Browser boundary, checkpoint streaming, market scope and ledger pagination."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from config import Settings, get_settings
from main import app
import main_ui_bridge as bridge


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    with TestClient(app) as value:
        value.headers["X-API-Key"] = get_settings().api_key.get_secret_value()
        yield value


def test_cors_preflight_allows_local_ui_without_bypassing_auth(client):
    preflight = client.options("/api/v1/agent/chat", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-api-key"})
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in preflight.headers
    rejected = client.options("/api/v1/agent/chat", headers={
        "Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST"})
    assert rejected.status_code == 400
    denied = client.get("/api/v1/llm/status", headers={"X-API-Key": "wrong", "Origin": "http://localhost:3000"})
    assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("origin", ["*", "https://*.example", "http://localhost:3000/", "https://name:password@example.com", "file:///tmp"])
def test_origins_must_be_explicit(origin):
    with pytest.raises(ValidationError):
        Settings(ui_origins=[origin])


def test_ui_routes_require_api_key(client):
    for path in ("/api/v1/trading/portfolio/metrics", "/api/v1/trading/orders",
                 "/api/v1/market/quote/AAPL", f"/api/v1/agent/requests/{uuid4()}/events?conversation_id=c"):
        assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401


def test_market_offline_scope_and_validated_quote(client, monkeypatch):
    settings = SimpleNamespace(llm_offline_mode=True, llm_public_tickers=["AAPL"])
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)
    calls = []
    from tests.test_phase2_mcp import quote
    monkeypatch.setattr(bridge, "fetch_stock_quote", lambda ticker: calls.append(ticker) or quote(ticker))
    assert client.get("/api/v1/market/quote/AAPL").status_code == 403
    settings.llm_offline_mode = False
    assert client.get("/api/v1/market/quote/SECRET").status_code == 403
    assert not calls
    result = client.get("/api/v1/market/quote/aapl")
    assert result.status_code == 200 and result.json()["ticker"] == "AAPL"
    assert calls == ["AAPL"]


def test_metrics_respect_offline_and_approved_scope(client, monkeypatch):
    monkeypatch.setattr(bridge, "get_settings", lambda: SimpleNamespace(
        llm_offline_mode=True, llm_public_tickers=["AAPL"]))
    async def metrics(**options):
        assert options == {"include_quotes": False, "approved_tickers": {"AAPL"}}
        return {"equity": None, "cash_balance": "100000.00"}
    monkeypatch.setattr(bridge, "get_portfolio_metrics", metrics)
    assert client.get("/api/v1/trading/portfolio/metrics").json()["equity"] is None


class ConnectedRequest:
    async def is_disconnected(self):
        return False


@pytest.mark.anyio
async def test_sse_reads_pending_and_final_checkpoints_without_execution(monkeypatch):
    snapshots = iter([
        {"steps": [{"call_id": "a", "status": "pending"}], "response": None},
        {"steps": [{"call_id": "a", "status": "succeeded"}], "response": None},
        {"steps": [], "response": {"answer": "Saved response"}},
    ])
    monkeypatch.setattr(bridge, "read_turn_progress", lambda *args: next(snapshots))
    async def no_wait(seconds):
        pass
    monkeypatch.setattr(bridge.anyio, "sleep", no_wait)
    events = [frame async for frame in bridge.progress_events(ConnectedRequest(), uuid4(), "conversation")]
    assert len(events) == 3
    assert '"pending"' in events[0] and '"succeeded"' in events[1]
    assert events[-1].startswith("event: completed\n")
    assert "Saved response" in events[-1]


@pytest.mark.anyio
async def test_sse_error_does_not_expose_private_exception(monkeypatch):
    def fail(*args):
        raise ValueError("SECRET_DATABASE_URL")
    monkeypatch.setattr(bridge, "read_turn_progress", fail)
    events = [frame async for frame in bridge.progress_events(ConnectedRequest(), uuid4(), "conversation")]
    assert len(events) == 1 and "stream_error" in events[0]
    assert "SECRET" not in events[0]


def test_progress_checks_conversation_and_omits_history(monkeypatch):
    class SessionStub:
        def __init__(self, *args):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, *args):
            return SimpleNamespace(conversation_id="right", response=None,
                                   state={"steps": [], "history": "PRIVATE_HISTORY", "input": "PRIVATE_INPUT"})
    monkeypatch.setattr(bridge, "Session", SessionStub)
    monkeypatch.setattr(bridge, "get_engine", lambda: None)
    assert "PRIVATE" not in json.dumps(bridge.read_turn_progress(uuid4(), "right"))
    with pytest.raises(ValueError):
        bridge.read_turn_progress(uuid4(), "wrong")
