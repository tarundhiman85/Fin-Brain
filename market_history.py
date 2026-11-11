"""Validated daily Yahoo OHLCV for bounded interactive charts."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field

from config import get_settings
from trading_service import TradingError, normalize_ticker


class Candle(BaseModel):
    time: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    open: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    close: float = Field(gt=0, allow_inf_nan=False)
    volume: int = Field(ge=0)


class CandleHistory(BaseModel):
    ticker: str
    currency: str
    period: str
    interval: str = "1d"
    source: str = "yahoo_finance"
    adjustment: str = "provider_ohlc_auto_adjust_false"
    fetched_at: datetime
    dropped_rows: int
    candles: list[Candle]


def fetch_candle_history(ticker: str, period: Literal["1y", "2y"] = "2y") -> CandleHistory:
    settings = get_settings()
    if settings.llm_offline_mode:
        raise TradingError("Market history is disabled by offline mode.")
    ticker = normalize_ticker(ticker)
    if ticker not in settings.llm_public_tickers:
        raise TradingError("Ticker is outside the configured public market scope.")
    if period not in {"1y", "2y"}:
        raise TradingError("Use a one-year or two-year daily history.")
    import yfinance as yf

    try:
        stock = yf.Ticker(ticker)
        bars = stock.history(period=period, interval="1d", auto_adjust=False, actions=False,
                             prepost=False, timeout=15, raise_errors=True)
        if bars.empty or len(bars) > 600:
            raise TradingError("No bounded daily history is available for this ticker.")
        metadata = stock.get_history_metadata()
        currency = metadata.get("currency", "")
        if metadata.get("symbol", "").upper() != ticker or len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise TradingError("History returned a different symbol or missing currency.")
        candles, dropped = {}, 0
        for index, row in bars.sort_index(kind="stable").iterrows():
            try:
                prices = [Decimal(str(row[key])) for key in ("Open", "High", "Low", "Close")]
                volume = Decimal(str(row["Volume"]))
                if (any(not value.is_finite() or value <= 0 or value >= Decimal("1e12") for value in prices)
                        or not volume.is_finite() or volume < 0 or volume > 9007199254740991 or volume != volume.to_integral_value()):
                    raise ValueError
                opening, high, low, close = prices
                if low > min(opening, close) or high < max(opening, close) or low > high:
                    raise ValueError
                # Daily bars use the exchange's session date, avoiding UTC date shifts.
                day = index.date().isoformat()
                candle = Candle(time=day, open=float(opening), high=float(high),
                                low=float(low), close=float(close), volume=int(volume))
                candles[day] = candle
            except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
                dropped += 1
        if not candles:
            raise TradingError("No valid OHLCV candles are available.")
        return CandleHistory(ticker=ticker, currency=currency.upper(), period=period,
                             fetched_at=datetime.now(timezone.utc), dropped_rows=dropped,
                             candles=[candles[day] for day in sorted(candles)])
    except TradingError:
        raise
    except Exception:
        raise TradingError("Market history unavailable. Check the symbol, connection and provider limits.") from None
