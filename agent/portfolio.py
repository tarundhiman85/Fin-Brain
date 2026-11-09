"""Snapshot-consistent portfolio accounting and indicative market valuation."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext

import anyio
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from config import get_settings
from database import get_engine
from models import VirtualOrder, VirtualPortfolio, VirtualPosition
from trading_service import CENT, UNIT, TradingError, fetch_stock_quote


def portfolio_snapshot() -> dict:
    portfolio_id = get_settings().paper_portfolio_id
    with localcontext() as context:
        context.prec = 60
        with Session(get_engine()) as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '15s'"))
            portfolio = session.get(VirtualPortfolio, portfolio_id)
            if portfolio is None:
                raise TradingError("Virtual portfolio not found.")
            positions = session.scalars(select(VirtualPosition).where(
                VirtualPosition.portfolio_id == portfolio_id).order_by(VirtualPosition.ticker)).all()
            orders = session.scalars(select(VirtualOrder).where(
                VirtualOrder.portfolio_id == portfolio_id, VirtualOrder.status == "CLOSED"
            ).order_by(VirtualOrder.id)).all()
            quantities, averages = {}, {}
            realized = Decimal("0")
            coverage = True
            for order in orders:
                held = quantities.get(order.ticker, Decimal("0"))
                average = averages.get(order.ticker, Decimal("0"))
                if order.order_type == "BUY":
                    averages[order.ticker] = (
                        (held * average + order.shares * order.buy_price) / (held + order.shares)
                    ).quantize(UNIT, rounding=ROUND_HALF_UP)
                    quantities[order.ticker] = held + order.shares
                else:
                    if order.shares > held:
                        coverage = False
                        continue
                    notional = (order.shares * order.buy_price).quantize(CENT, rounding=ROUND_HALF_UP)
                    if order.execution_metadata and "settled_notional" in order.execution_metadata:
                        notional = Decimal(order.execution_metadata["settled_notional"])
                    realized += notional - (order.shares * average).quantize(CENT, rounding=ROUND_HALF_UP)
                    quantities[order.ticker] = held - order.shares
            actual = {p.ticker: p.shares for p in positions}
            if any(quantities.get(ticker, 0) != actual.get(ticker, 0)
                   for ticker in quantities.keys() | actual.keys()):
                coverage = False
            if any(p.shares > 0 and averages.get(p.ticker) != p.buy_price for p in positions):
                coverage = False
            holdings = [
                {"ticker": p.ticker, "shares": str(p.shares), "buy_price": str(p.buy_price),
                 "cost_basis": str((p.shares * p.buy_price).quantize(CENT, rounding=ROUND_HALF_UP))}
                for p in positions if p.status == "OPEN"
            ]
            return {
                "id": portfolio.id, "currency": portfolio.currency,
                "cash_balance": str(portfolio.cash_balance),
                "updated_at": portfolio.updated_at.isoformat(),
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
                "holdings": holdings, "realized_pnl": str(realized) if coverage else None,
                "ledger_coverage_complete": coverage,
            }


async def get_portfolio_metrics() -> dict:
    snapshot = await anyio.to_thread.run_sync(portfolio_snapshot)
    limiter = anyio.CapacityLimiter(4)
    async def mark(holding):
        try:
            quote = await anyio.to_thread.run_sync(
                lambda: fetch_stock_quote(holding["ticker"]), limiter=limiter)
            if quote.currency != snapshot["currency"] or quote.ticker != holding["ticker"]:
                raise TradingError("Quote currency or ticker does not match the holding.")
            with localcontext() as context:
                context.prec = 60
                value = (Decimal(holding["shares"]) * quote.price).quantize(CENT, rounding=ROUND_HALF_UP)
                holding.update(
                    market_price=str(quote.price), market_value=str(value),
                    unrealized_pnl=str(value - Decimal(holding["cost_basis"])),
                    quote_as_of=quote.as_of.isoformat(),
                    quote_stale=(datetime.now(timezone.utc) - quote.as_of).total_seconds()
                    > get_settings().max_trade_quote_age_seconds,
                    quote_error=None)
        except Exception:
            holding.update(market_price=None, market_value=None, unrealized_pnl=None,
                           quote_as_of=None, quote_stale=None, quote_error="Market valuation unavailable.")
    async with anyio.create_task_group() as group:
        for holding in snapshot["holdings"]:
            group.start_soon(mark, holding)
    with localcontext() as context:
        context.prec = 60
        complete = all(h["market_value"] is not None for h in snapshot["holdings"])
        cost_basis = sum((Decimal(h["cost_basis"]) for h in snapshot["holdings"]), Decimal("0"))
        market_value = sum((Decimal(h["market_value"]) for h in snapshot["holdings"]), Decimal("0")) if complete else None
        unrealized = market_value - cost_basis if complete else None
        realized = Decimal(snapshot["realized_pnl"]) if snapshot["realized_pnl"] is not None else None
        snapshot.update(
            holdings_cost_basis=str(cost_basis), valuation_complete=complete,
            market_value=str(market_value) if complete else None,
            equity=str(Decimal(snapshot["cash_balance"]) + market_value) if complete else None,
            unrealized_pnl=str(unrealized) if complete else None,
            total_pnl=str(realized + unrealized) if complete and realized is not None else None,
        )
    return snapshot
