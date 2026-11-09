"""Yahoo quote normalization and atomic average-cost virtual trade execution."""
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from config import get_settings
from database import get_engine
from models import VirtualOrder, VirtualPortfolio, VirtualPosition

CENT = Decimal("0.01")
UNIT = Decimal("0.00000001")
MAX_CASH = Decimal("1e18")


class TradingError(ValueError):
    """An anticipated trading rejection, safe to show to an MCP client."""


def normalize_ticker(ticker: str) -> str:
    ticker = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z^][A-Z0-9.^=-]{0,19}", ticker):
        raise TradingError("Use a valid Yahoo ticker of at most 20 characters.")
    return ticker


class StockQuote(BaseModel):
    ticker: str
    price: Decimal = Field(gt=0, lt=Decimal("1e12"))
    currency: str
    instrument_type: str
    as_of: datetime
    fetched_at: datetime
    age_seconds: float
    source: str = "yahoo_finance"
    price_basis: str = "regular_session_1m_close"
    session_open: Decimal
    session_high: Decimal
    session_low: Decimal
    session_volume: int


def fetch_stock_quote(ticker: str) -> StockQuote:
    """Fetch timestamped regular-session bars with finite network timeouts."""
    import yfinance as yf

    ticker = normalize_ticker(ticker)
    try:
        stock = yf.Ticker(ticker)
        bars = stock.history(
            period="5d", interval="1m", auto_adjust=False, actions=False,
            prepost=False, timeout=15, raise_errors=True,
        ).dropna(subset=["Close"])
        if bars.empty:
            raise TradingError("No market quote is available for this ticker.")
        bars = bars.sort_index()
        metadata = stock.get_history_metadata()
        if metadata.get("symbol", "").upper() != ticker:
            raise TradingError("Market data returned a different or missing ticker.")
        currency = metadata.get("currency")
        if not currency:
            raise TradingError("Market data did not include a currency.")
        as_of = bars.index[-1].to_pydatetime()
        if as_of.tzinfo is None:
            raise TradingError("Market data did not include a timezone.")
        as_of = as_of.astimezone(timezone.utc)
        fetched_at = datetime.now(timezone.utc)
        age = (fetched_at - as_of).total_seconds()
        if age < -60:
            raise TradingError("Market data timestamp is in the future.")
        latest_session = bars[bars.index.date == bars.index[-1].date()]
        price = Decimal(str(bars.iloc[-1]["Close"])).quantize(UNIT, rounding=ROUND_HALF_UP)
        return StockQuote(
            ticker=ticker, price=price, currency=currency,
            instrument_type=metadata.get("instrumentType", "UNKNOWN"),
            as_of=as_of, fetched_at=fetched_at, age_seconds=max(0, age),
            session_open=Decimal(str(latest_session.iloc[0]["Open"])),
            session_high=Decimal(str(latest_session["High"].max())),
            session_low=Decimal(str(latest_session["Low"].min())),
            session_volume=int(latest_session["Volume"].sum()),
        )
    except TradingError:
        raise
    except Exception:
        # Provider errors can include URLs, cookies, response bodies and symbols.
        raise TradingError(
            "Quote unavailable. Check the ticker, network access, and Yahoo rate limits.") from None


def validate_trade(ticker: str, shares: float, order_type: str, request_id: str | None):
    ticker = normalize_ticker(ticker)
    order_type = order_type.strip().upper()
    if order_type not in {"BUY", "SELL"}:
        raise TradingError("order_type must be BUY or SELL.")
    try:
        quantity = Decimal(str(shares))
        if (isinstance(shares, bool) or not quantity.is_finite() or quantity <= 0
                or quantity >= Decimal("1e16") or quantity != quantity.quantize(UNIT)):
            raise ValueError
        identifier = UUID(request_id) if request_id is not None else uuid4()
    except (ValueError, TypeError, InvalidOperation, AttributeError):
        raise TradingError(
            "Shares must be positive with at most 8 decimals and request_id must be a UUID.") from None
    return ticker, quantity, order_type, identifier


def previous_receipt(session: Session, identifier: UUID, portfolio_id: int,
                     ticker: str, quantity: Decimal, side: str) -> dict | None:
    previous = session.scalar(select(VirtualOrder).where(VirtualOrder.request_id == identifier))
    if previous is None:
        return None
    if (previous.portfolio_id != portfolio_id or previous.ticker != ticker
            or previous.shares != quantity or previous.order_type != side):
        raise TradingError("request_id was already used with different trade arguments.")
    if previous.execution_metadata is None:
        raise TradingError("Existing order has no execution receipt. Inspect the ledger.")
    return dict(previous.execution_metadata, replayed=True)


def validate_execution_quote(quote: StockQuote, ticker: str, currency: str) -> None:
    if quote.ticker != ticker or quote.currency != currency:
        raise TradingError("Quote ticker/currency must match the portfolio. FX conversion is required.")
    if quote.instrument_type not in {"EQUITY", "ETF"}:
        raise TradingError("Paper execution supports stocks and ETFs.")
    age = (datetime.now(timezone.utc) - quote.as_of).total_seconds()
    if age < -60 or age > get_settings().max_trade_quote_age_seconds:
        raise TradingError("Quote is stale or invalid. Request a fresh market quote.")


def apply_fill(session: Session, portfolio: VirtualPortfolio, position: VirtualPosition | None,
               ticker: str, quantity: Decimal, side: str, quote: StockQuote,
               identifier: UUID, *, max_buy_price: Decimal | None = None,
               min_sell_price: Decimal | None = None, max_notional: Decimal | None = None) -> dict:
    """Apply one fill while the caller holds the portfolio's FOR UPDATE lock."""
    validate_execution_quote(quote, ticker, portfolio.currency)
    price = quote.price.quantize(UNIT, rounding=ROUND_HALF_UP)
    notional = (quantity * price).quantize(CENT, rounding=ROUND_HALF_UP)
    if notional < CENT:
        raise TradingError("Trade value must settle to at least 0.01 in the portfolio currency.")
    if side == "BUY" and max_buy_price is not None and price > max_buy_price:
        raise TradingError("Fill price exceeds the maximum buy price.")
    if side == "SELL" and min_sell_price is not None and price < min_sell_price:
        raise TradingError("Fill price is below the minimum sell price.")
    if max_notional is not None and notional > max_notional:
        raise TradingError("Settled notional exceeds the trade budget.")
    now = datetime.now(timezone.utc)
    held = position.shares if position is not None and position.status == "OPEN" else Decimal("0")
    if side == "BUY":
        if portfolio.cash_balance < notional:
            raise TradingError("Insufficient virtual cash.")
        updated_shares = held + quantity
        if updated_shares >= Decimal("1e16"):
            raise TradingError("Position size exceeds ledger precision.")
        average = ((held * position.buy_price if held else Decimal("0")) + quantity * price)
        average = (average / updated_shares).quantize(UNIT, rounding=ROUND_HALF_UP)
        if position is None:
            position = VirtualPosition(portfolio_id=portfolio.id, ticker=ticker)
            session.add(position)
        position.shares = updated_shares
        position.buy_price = average
        position.status = "OPEN"
        position.timestamp = now
        portfolio.cash_balance -= notional
    else:
        if quantity > held:
            raise TradingError("Insufficient virtual shares.")
        if portfolio.cash_balance + notional >= MAX_CASH:
            raise TradingError("Cash balance would exceed ledger precision.")
        position.shares = held - quantity
        position.status = "OPEN" if position.shares else "CLOSED"
        position.timestamp = now
        portfolio.cash_balance += notional
    portfolio.updated_at = now
    order = VirtualOrder(
        portfolio_id=portfolio.id, request_id=identifier, ticker=ticker,
        shares=quantity, buy_price=price, order_type=side, status="CLOSED", timestamp=now)
    session.add(order)
    session.flush()
    receipt = {
        "order_id": order.id, "request_id": str(identifier), "portfolio_id": portfolio.id,
        "ticker": ticker, "shares": format(quantity, "f"), "order_type": side,
        "status": "CLOSED", "fill_price": format(price, "f"),
        "settled_notional": format(notional, ".2f"), "currency": portfolio.currency,
        "cash_balance": format(portfolio.cash_balance, ".2f"),
        "position_shares": format(position.shares, "f"),
        "position_buy_price": format(position.buy_price, "f"),
        "executed_at": now.isoformat(), "quote_as_of": quote.as_of.isoformat(),
        "price_basis": quote.price_basis, "replayed": False,
    }
    order.execution_metadata = receipt
    return receipt


def execute_trade(ticker: str, shares: float, order_type: str,
                  request_id: str | None = None, *, max_buy_price: Decimal | None = None,
                  min_sell_price: Decimal | None = None, max_notional: Decimal | None = None) -> dict:
    ticker, quantity, side, identifier = validate_trade(ticker, shares, order_type, request_id)
    limits = {"max_buy_price": max_buy_price, "min_sell_price": min_sell_price,
              "max_notional": max_notional}
    try:
        limits = {name: Decimal(str(value)) if value is not None else None
                  for name, value in limits.items()}
        if any(value is not None and (not value.is_finite() or value <= 0)
               for value in limits.values()):
            raise ValueError
    except (ValueError, InvalidOperation):
        raise TradingError("Trade limits must be finite positive decimal values.") from None
    portfolio_id = get_settings().paper_portfolio_id
    # A committed retry can succeed even when Yahoo is currently unavailable.
    if request_id is not None:
        with Session(get_engine()) as session:
            session.execute(text("SET LOCAL statement_timeout = '15s'"))
            replay = previous_receipt(session, identifier, portfolio_id, ticker, quantity, side)
            if replay is not None:
                return replay
    quote = fetch_stock_quote(ticker)
    # Do network I/O before acquiring the portfolio lock.
    with localcontext() as decimal_context:
        decimal_context.prec = 60
        with Session(get_engine()) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '10s'"))
            session.execute(text("SET LOCAL statement_timeout = '15s'"))
            portfolio = session.scalar(select(VirtualPortfolio).where(
                VirtualPortfolio.id == portfolio_id).with_for_update())
            if portfolio is None:
                raise TradingError("Virtual portfolio not found. Initialize the Phase 1 database.")
            # Recheck after locking to serialize concurrent retries and first-position creation.
            replay = previous_receipt(session, identifier, portfolio_id, ticker, quantity, side)
            if replay is not None:
                return replay
            position = session.scalar(select(VirtualPosition).where(
                VirtualPosition.portfolio_id == portfolio_id, VirtualPosition.ticker == ticker
            ).with_for_update())
            receipt = apply_fill(session, portfolio, position, ticker, quantity, side, quote, identifier, **limits)
        # The receipt is returned only after the transaction commits.
        return receipt
