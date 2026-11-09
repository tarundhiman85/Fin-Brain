"""Real transaction/concurrency tests against a disposable initialized database."""
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session

from models import VirtualOrder, VirtualPortfolio, VirtualPosition
from tests.test_phase2_mcp import quote
import trading_service
from trading_service import TradingError, execute_trade
from upgrade_phase2 import upgrade

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"),
                                reason="TEST_DATABASE_URL not provided")


@pytest.fixture
def ledger(monkeypatch):
    engine = create_engine(os.environ["TEST_DATABASE_URL"], hide_parameters=True)
    portfolio_id = int(uuid4().hex[:7], 16)
    with Session(engine) as session, session.begin():
        session.add(VirtualPortfolio(id=portfolio_id, cash_balance=Decimal("1000.00"), currency="USD"))
    monkeypatch.setattr(trading_service, "get_engine", lambda: engine)
    monkeypatch.setattr(trading_service, "get_settings", lambda: SimpleNamespace(
        paper_portfolio_id=portfolio_id, max_trade_quote_age_seconds=1200))
    monkeypatch.setattr(trading_service, "fetch_stock_quote", lambda ticker: quote(ticker))
    try:
        yield engine, portfolio_id
    finally:
        with Session(engine) as session, session.begin():
            session.execute(delete(VirtualOrder).where(VirtualOrder.portfolio_id == portfolio_id))
            session.execute(delete(VirtualPosition).where(VirtualPosition.portfolio_id == portfolio_id))
            session.execute(delete(VirtualPortfolio).where(VirtualPortfolio.id == portfolio_id))
        engine.dispose()


def test_round_trip_retries_and_legacy_order_compatibility(ledger, monkeypatch):
    engine, portfolio_id = ledger
    upgrade(engine)
    upgrade(engine)
    # Existing Phase 1 orders with no retry metadata remain valid.
    with Session(engine) as session, session.begin():
        session.add(VirtualOrder(portfolio_id=portfolio_id, ticker="LEGACY", shares=1,
                                 buy_price=1, order_type="BUY", status="CLOSED"))
    key = str(uuid4())
    first = execute_trade("AAPL", 2.5, "BUY", key)
    monkeypatch.setattr(trading_service, "fetch_stock_quote",
                        lambda ticker: (_ for _ in ()).throw(RuntimeError("offline")))
    replay = execute_trade("AAPL", 2.5, "BUY", key)
    assert replay["order_id"] == first["order_id"] and replay["replayed"]
    with pytest.raises(TradingError, match="different trade arguments"):
        execute_trade("AAPL", 3, "BUY", key)
    monkeypatch.setattr(trading_service, "fetch_stock_quote", lambda ticker: quote(ticker, "120"))
    sale = execute_trade("AAPL", 2.5, "SELL", str(uuid4()))
    assert sale["cash_balance"] == "1050.00" and Decimal(sale["position_shares"]) == 0
    with Session(engine) as session:
        position = session.scalar(select(VirtualPosition).where(
            VirtualPosition.portfolio_id == portfolio_id))
        assert position.status == "CLOSED"


@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_buys_cannot_overspend_or_duplicate(ledger, monkeypatch, same_request):
    engine, portfolio_id = ledger
    barrier = Barrier(2)
    def synchronized_quote(ticker):
        barrier.wait(timeout=10)
        return quote(ticker)
    monkeypatch.setattr(trading_service, "fetch_stock_quote", synchronized_quote)
    first_key = str(uuid4())
    keys = [first_key, first_key if same_request else str(uuid4())]
    def buy(key):
        try:
            return execute_trade("AAPL", 7, "BUY", key)
        except TradingError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(buy, keys))
    if same_request:
        assert all(isinstance(result, dict) for result in results)
        assert sum(result["replayed"] for result in results) == 1
    else:
        assert sum(isinstance(result, TradingError) for result in results) == 1
    with Session(engine) as session:
        assert session.get(VirtualPortfolio, portfolio_id).cash_balance == Decimal("300")
        positions = session.scalars(select(VirtualPosition).where(
            VirtualPosition.portfolio_id == portfolio_id)).all()
        assert len(positions) == 1 and positions[0].shares == Decimal("7")
        orders = session.scalars(select(VirtualOrder).where(
            VirtualOrder.portfolio_id == portfolio_id)).all()
        assert len(orders) == 1


def test_concurrent_sells_cannot_oversell(ledger, monkeypatch):
    engine, portfolio_id = ledger
    execute_trade("AAPL", 5, "BUY")
    barrier = Barrier(2)
    def synchronized_quote(ticker):
        barrier.wait(timeout=10)
        return quote(ticker)
    monkeypatch.setattr(trading_service, "fetch_stock_quote", synchronized_quote)
    def sell(_):
        try:
            return execute_trade("AAPL", 4, "SELL")
        except TradingError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(sell, range(2)))
    assert sum(isinstance(result, TradingError) for result in results) == 1
    with Session(engine) as session:
        assert session.get(VirtualPortfolio, portfolio_id).cash_balance == Decimal("900")
        position = session.scalar(select(VirtualPosition).where(
            VirtualPosition.portfolio_id == portfolio_id))
        assert position.shares == Decimal("1")


def test_database_failure_rolls_back_entire_fill(ledger):
    engine, portfolio_id = ledger
    def fail_order(connection, cursor, statement, parameters, context, executemany):
        if "INSERT INTO virtual_orders" in statement:
            raise RuntimeError("injected failure")
    event.listen(engine, "before_cursor_execute", fail_order)
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            execute_trade("AAPL", 2, "BUY")
    finally:
        event.remove(engine, "before_cursor_execute", fail_order)
    with Session(engine) as session:
        assert session.get(VirtualPortfolio, portfolio_id).cash_balance == Decimal("1000")
        assert session.scalar(select(VirtualPosition.id).where(
            VirtualPosition.portfolio_id == portfolio_id)) is None
        assert session.scalar(select(VirtualOrder.id).where(
            VirtualOrder.portfolio_id == portfolio_id)) is None

def test_mcp_search_and_trade_use_real_database(ledger, monkeypatch):
    import anyio
    from unittest.mock import Mock
    from mcp_servers.finance_kb_mcp import mcp as finance_mcp
    from mcp_servers.market_trading_mcp import mcp as trading_mcp
    from models import EmbeddingConfiguration, FinancialDocumentChunk

    engine, portfolio_id = ledger
    vector = [1.0] + [0.0] * 767
    document_id = uuid4()
    with Session(engine) as session, session.begin():
        identity = session.get(EmbeddingConfiguration, 1).identity
        session.add(FinancialDocumentChunk(
            id=document_id, content="Mock grocery purchase for MCP regression",
            document_metadata={"amount": "-12.30", "category": "Groceries"},
            fingerprint=uuid4().hex, embedding=vector))
    embedder = Mock(identity=identity)
    embedder.embed.return_value = [vector]
    monkeypatch.setattr("mcp_servers.finance_kb_mcp.get_engine", lambda: engine)
    monkeypatch.setattr("mcp_servers.finance_kb_mcp.get_embedder", lambda: embedder)

    async def call_tools():
        search = await finance_mcp.call_tool("search_financial_kb", {
            "query": "groceries", "top_k": 1})
        assert search.structured_content["results"][0]["id"] == str(document_id)
        assert search.structured_content["results"][0]["similarity"] == pytest.approx(1)
        trade = await trading_mcp.call_tool("execute_paper_trade", {
            "ticker": "AAPL", "shares": 1.25, "order_type": "BUY",
            "request_id": str(uuid4())})
        assert trade.structured_content["cash_balance"] == "875.00"

    try:
        anyio.run(call_tools)
        # Verify the existing HTTP endpoint sees the same ledger changes.
        from fastapi.testclient import TestClient
        from config import get_settings
        from database import get_session
        from main import app
        def actual_session():
            with Session(engine) as session:
                yield session
        app.dependency_overrides[get_session] = actual_session
        try:
            with TestClient(app) as api:
                result = api.get(
                    f"/api/v1/trading/portfolio?portfolio_id={portfolio_id}",
                    headers={"X-API-Key": get_settings().api_key.get_secret_value()})
                assert result.status_code == 200
                assert result.json()["cash_balance"] == "875.00"
                assert Decimal(result.json()["holdings"][0]["shares"]) == Decimal("1.25")
        finally:
            app.dependency_overrides.clear()
    finally:
        with Session(engine) as session, session.begin():
            session.execute(delete(FinancialDocumentChunk).where(
                FinancialDocumentChunk.id == document_id))

def test_migration_upgrades_legacy_columns_and_preserves_rows(ledger):
    from contextlib import contextmanager
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    engine, _ = ledger
    # A temporary table shadows the real ledger on this connection only.
    with engine.connect() as connection, connection.begin():
        connection.execute(text(
            "CREATE TEMP TABLE virtual_orders (id INTEGER PRIMARY KEY, ticker TEXT) ON COMMIT DROP"))
        connection.execute(text("INSERT INTO virtual_orders (id, ticker) VALUES (1, 'LEGACY')"))
        @contextmanager
        def same_connection():
            yield connection
        adapter = SimpleNamespace(begin=same_connection)
        upgrade(adapter)
        upgrade(adapter)
        row = connection.execute(text(
            "SELECT ticker, request_id, execution_metadata FROM virtual_orders WHERE id = 1")).one()
        assert tuple(row) == ("LEGACY", None, None)
        key = uuid4()
        connection.execute(text(
            "INSERT INTO virtual_orders (id, request_id) VALUES (2, :key)"), {"key": key})
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text(
                    "INSERT INTO virtual_orders (id, request_id) VALUES (3, :key)"), {"key": key})
