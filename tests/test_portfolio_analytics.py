"""Exact settled-exit analytics using real PostgreSQL and the paper fill engine."""
import os
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from agent.portfolio import portfolio_snapshot
from models import VirtualPosition
from tests.test_agent_postgres import agent_env
from tests.test_phase2_postgres import ledger
from tests.test_phase2_mcp import quote
from trading_service import execute_trade

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not provided")


def test_win_loss_breakeven_and_realized_capital_drawdown(agent_env, monkeypatch):
    current = ["100"]
    monkeypatch.setattr("trading_service.fetch_stock_quote", lambda ticker: quote(ticker, current[0]))
    execute_trade("AAPL", 4, "BUY")
    for price in ("120", "80", "100"):
        current[0] = price
        execute_trade("AAPL", 1, "SELL")
    result = portfolio_snapshot()["analytics"]
    assert result["coverage_complete"] and result["initial_capital"] == "1000.00"
    assert (result["wins"], result["losses"], result["breakeven"], result["completed_exits"]) == (1, 1, 1, 3)
    assert Decimal(result["win_loss_ratio"]) == 1
    assert result["win_rate_percent"] == "50.00"
    assert result["max_drawdown_percent"] == "1.96"


def test_partial_exit_uses_average_cost_and_zero_loss_ratio_is_null(agent_env, monkeypatch):
    current = ["100"]
    monkeypatch.setattr("trading_service.fetch_stock_quote", lambda ticker: quote(ticker, current[0]))
    execute_trade("AAPL", 1, "BUY")
    current[0] = "200"
    execute_trade("AAPL", 1, "BUY")
    current[0] = "180"
    execute_trade("AAPL", 1, "SELL")
    snapshot = portfolio_snapshot()
    assert Decimal(snapshot["realized_pnl"]) == 30
    assert snapshot["analytics"]["wins"] == 1
    assert snapshot["analytics"]["win_loss_ratio"] is None
    assert snapshot["analytics"]["max_drawdown_percent"] == "0.00"


def test_no_sells_and_missing_legacy_orders_do_not_invent_metrics(agent_env):
    engine, settings, _ = agent_env
    analytics = portfolio_snapshot()["analytics"]
    assert analytics["completed_exits"] == 0 and analytics["max_drawdown_percent"] is None
    with Session(engine) as session, session.begin():
        session.add(VirtualPosition(portfolio_id=settings.paper_portfolio_id, ticker="LEGACY",
                                    shares=1, buy_price=100, status="OPEN"))
    analytics = portfolio_snapshot()["analytics"]
    assert not analytics["coverage_complete"]
    assert analytics["wins"] is None and analytics["win_loss_ratio"] is None
    assert analytics["max_drawdown_percent"] is None
