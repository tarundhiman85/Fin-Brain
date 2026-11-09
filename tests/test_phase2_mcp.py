"""Protocol, quote validation and trading regression tests."""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError

import trading_service
from models import VirtualPortfolio, VirtualPosition
from trading_service import (
    StockQuote, TradingError, apply_fill, fetch_stock_quote, validate_trade,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def quote(ticker="AAPL", price="100.00", age=0, currency="USD"):
    now = datetime.now(timezone.utc)
    return StockQuote(ticker=ticker, price=Decimal(price), currency=currency,
                      instrument_type="EQUITY", as_of=now - timedelta(seconds=age),
                      fetched_at=now, age_seconds=age, session_open=Decimal(price),
                      session_high=Decimal(price), session_low=Decimal(price), session_volume=100)


@pytest.mark.parametrize("shares", [0, -1, float("nan"), float("inf"), 0.000000001, True])
def test_invalid_share_quantities(shares):
    with pytest.raises(TradingError):
        validate_trade("AAPL", shares, "BUY", None)


def test_trade_normalization_and_request_id():
    key = str(uuid4())
    ticker, quantity, side, identifier = validate_trade(" aapl ", 1.25, "buy", key)
    assert (ticker, quantity, side, str(identifier)) == ("AAPL", Decimal("1.25"), "BUY", key)
    for args in [("AAPL", 1, "HOLD", None), ("***", 1, "BUY", None),
                 ("AAPL", 1, "BUY", "not-a-uuid")]:
        with pytest.raises(TradingError):
            validate_trade(*args)


def test_average_cost_sell_and_reopen():
    portfolio = VirtualPortfolio(id=1, cash_balance=Decimal("1000"), currency="USD")
    position = VirtualPosition(portfolio_id=1, ticker="AAPL", shares=Decimal("2"),
                               buy_price=Decimal("50"), status="OPEN")
    session = Mock()
    apply_fill(session, portfolio, position, "AAPL", Decimal("2"), "BUY", quote(), uuid4())
    assert position.shares == Decimal("4")
    assert position.buy_price == Decimal("75")
    assert portfolio.cash_balance == Decimal("800")
    apply_fill(session, portfolio, position, "AAPL", Decimal("4"), "SELL", quote(), uuid4())
    assert position.status == "CLOSED" and position.shares == 0
    apply_fill(session, portfolio, position, "AAPL", Decimal("1"), "BUY", quote(price="120"), uuid4())
    assert position.status == "OPEN" and position.buy_price == Decimal("120")


@pytest.mark.parametrize("bad_quote", [quote(age=3600), quote(currency="EUR")])
def test_rejects_stale_and_currency_mismatched_fills(bad_quote):
    portfolio = VirtualPortfolio(id=1, cash_balance=Decimal("1000"), currency="USD")
    with pytest.raises(TradingError):
        apply_fill(Mock(), portfolio, None, "AAPL", Decimal("1"), "BUY", bad_quote, uuid4())
    assert portfolio.cash_balance == Decimal("1000")


def test_rejects_sub_cent_fill():
    portfolio = VirtualPortfolio(id=1, cash_balance=Decimal("1000"), currency="USD")
    with pytest.raises(TradingError):
        apply_fill(Mock(), portfolio, None, "AAPL", Decimal("0.00000001"), "BUY", quote(), uuid4())


def test_yahoo_bar_normalization(monkeypatch):
    import pandas as pd
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    frame = pd.DataFrame(
        {"Open": [99.0, 100.0], "High": [100.0, 102.0], "Low": [98.0, 99.0],
         "Close": [99.5, 101.25], "Volume": [50, 100]},
        index=pd.DatetimeIndex([now - timedelta(minutes=1), now]))
    stock = Mock()
    stock.history.return_value = frame
    stock.get_history_metadata.return_value = {
        "symbol": "AAPL", "currency": "USD", "instrumentType": "EQUITY"}
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: stock)
    result = fetch_stock_quote("aapl")
    assert result.price == Decimal("101.25000000")
    assert result.session_volume == 150 and result.session_high == Decimal("102")
    assert result.as_of.tzinfo is not None
    assert stock.history.call_args.kwargs["auto_adjust"] is False
    assert stock.history.call_args.kwargs["timeout"] == 15


@pytest.mark.anyio
async def test_market_tool_structured_output_and_safe_error(monkeypatch):
    from mcp_servers.market_trading_mcp import mcp
    monkeypatch.setattr("mcp_servers.market_trading_mcp.fetch_stock_quote", lambda ticker: quote())
    result = await mcp.call_tool("get_stock_quote", {"ticker": "AAPL"})
    assert result.structured_content["price"] == "100.00"
    def failed(ticker):
        raise RuntimeError("sensitive backend error with database credentials")
    monkeypatch.setattr("mcp_servers.market_trading_mcp.fetch_stock_quote", failed)
    with pytest.raises(ToolError) as error:
        await mcp.call_tool("get_stock_quote", {"ticker": "AAPL"})
    assert "sensitive" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("script,expected,tool,args", [
    ("finance_kb_mcp.py", {"search_financial_kb"}, "search_financial_kb", {"query": ""}),
    ("market_trading_mcp.py", {"get_stock_quote", "execute_paper_trade"},
     "get_stock_quote", {"ticker": "***"}),
])
async def test_real_stdio_handshake_from_different_directory(tmp_path, script, expected, tool, args):
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable, args=[str(root / "mcp_servers" / script)],
        cwd=tmp_path, env={
            "DATABASE_URL": os.environ.get(
                "DATABASE_URL", "postgresql+psycopg://test:test@localhost/test"),
            "API_KEY": "mcp-protocol-test-" + "x" * 40,
        },
    )
    with anyio.fail_after(30):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == expected
                result = await session.call_tool(tool, args)
                assert result.is_error

@pytest.mark.anyio
async def test_mcp_rejects_boolean_shares_before_execution(monkeypatch):
    from mcp_servers.market_trading_mcp import mcp
    execute = Mock()
    monkeypatch.setattr("mcp_servers.market_trading_mcp.execute_trade", execute)
    with pytest.raises(ToolError):
        await mcp.call_tool("execute_paper_trade", {
            "ticker": "AAPL", "shares": True, "order_type": "BUY"})
    execute.assert_not_called()
