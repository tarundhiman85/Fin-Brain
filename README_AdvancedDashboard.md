# Advanced trading dashboard

Phase 7 adds daily candlesticks, volume, SMA 50/200, Wilder RSI 14, a verified
watchlist manager and realized paper-trade analytics to the existing dashboard.
The implementation is in the shared TradingDashboard component rendered by
`frontend/app/dashboard/page.tsx`. No schema migration is needed.

## Install and launch

From the repository root, install the existing Python requirements if necessary.
The history endpoint reuses the installed yfinance dependency.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Restart FastAPI after applying these changes. For the existing demo container,
which mounts the workspace source, use the following command.

```powershell
docker restart finbrain-demo-api
```

For a regular local installation, use the existing database/API configuration and
start FastAPI using the Phase 6 instructions.

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Install the frontend dependencies and start the UI in another terminal.

```powershell
cd frontend
npm ci
npm run dev
```

The package manifest and lockfile include `lightweight-charts` 5.3.0. To add the
chart package separately in another checkout, use the following command.

```powershell
npm install lightweight-charts@5.3.0
```

Open `http://127.0.0.1:3000/dashboard`, enter your backend API key in the sidebar,
and connect. The key stays in tab memory as in Phase 6. Historical market access
requires `LLM_OFFLINE_MODE=false`. Local Only continues to control LLM inference.
Market data requests use Yahoo through the backend and require network access.

## Select an asset and inspect history

- Click a symbol in Active positions or Market watch, or use the Chart ticker dropdown.
- Drag the price chart to pan and scroll/pinch to zoom.
- Hover or use touch crosshair tracking to inspect the session's open, high, low,
  close and volume values in the legend.
- Select 3MO, 6MO, 1Y or 2Y to change the visible date range.
- Enable SMA 50, SMA 200 or RSI 14 using the indicator checkboxes.
- Use the history refresh button to fetch new candles.
- Open View OHLCV data for an accessible table of the latest 30 sessions.

The chart fetches two years of daily data once per selected ticker or manual
refresh. Range changes and indicator toggles use the loaded history. The extra
history warms up the SMA 200 even when the visible range is three months.
SMA begins after its required number of valid bars. New listings may have less
than 200 bars and show a warmup message. Volume occupies a separate pane. RSI
occupies another pane with 30/70 reference levels and a 0–100 price scale.

RSI uses Wilder smoothing. It averages gains and losses over the first 14 price
changes, then applies the recursive Wilder average. A flat initial window yields
50, an all-gain window yields 100, and an all-loss window yields 0. Indicators
use closing prices and trading bars rather than calendar-day padding.

Charts resize with their container through the library's ResizeObserver support.
Changing tickers aborts the previous request and disposes the old chart. Toggling
indicators preserves the visible date range. Chart instances and subscriptions
are removed on unmount. The page retains the library's TradingView attribution
and links to its creator. [Lightweight Charts](https://www.tradingview.com/lightweight-charts/)

Daily candle dates retain the exchange's session date. Fetched timestamps use
UTC. Data uses Yahoo OHLC with `auto_adjust=False`; dividend adjustments are not
applied by this endpoint. The current session's candle can change. The chart
shows provider history and does not imply exchange-real-time prices.

## Manage the watchlist

Enter a symbol in Add ticker and submit it. Symbols are normalized to uppercase
and checked against the backend's `LLM_PUBLIC_TICKERS` scope. A quote request must
succeed for that symbol before the card is added. Duplicate symbols, a full
six-card watchlist and provider failures produce inline feedback. Remove deletes
the card from the current dashboard state. Selecting an asset for the chart is
independent from keeping a card on the watchlist.

For another public ticker, add its symbol to `LLM_PUBLIC_TICKERS` in the backend
configuration and restart/reconnect the backend and browser connection. This
preserves the existing explicit public-market scope policy. Watchlist state
remains in the current dashboard view and resets when that view is remounted.
The manager performs read-only market requests.

## Portfolio analytics definitions

Analytics reuse the existing PostgreSQL snapshot and average-cost ledger replay.
They cover the configured `PAPER_PORTFOLIO_ID`, processing settled orders in ledger
ID order. A completed exit is one settled SELL fill. Partial sells are separate
exits. Buy orders and open positions do not count as completed exits.

| Metric | Calculation |
| --- | --- |
| Winning / losing exits | Number of SELL fills with positive / negative realized P&L |
| Breakeven exits | SELL fills with zero realized P&L after cent rounding |
| Win / loss ratio | Winning exit count divided by losing exit count |
| Win rate | Wins divided by wins plus losses, excluding breakeven exits |
| Max drawdown · realized capital | Largest percentage decline from a peak in inferred initial capital plus cumulative realized P&L, evaluated at SELL fills |

Each sell's realized P&L uses its saved settled proceeds and the average cost of
shares sold. Legacy rows without settlement metadata fall back to the existing
cent-rounded shares-times-price calculation. The statistics reconcile replayed
quantities and average costs with current positions. Incomplete legacy coverage
makes the analytics unavailable.

Initial capital is inferred as current virtual cash plus settled buys minus
settled sells. This assumes the recorded order ledger explains cash movements.
Undocumented deposits, withdrawals or manual balance edits are outside this
calculation. Full historical marked portfolio equity is not stored by earlier
phases. The realized-capital drawdown excludes unrealized price changes in open
holdings and should be interpreted with that scope.

No losing exits produces a null numeric ratio with the UI text No losing exits
when wins exist. No completed sells produces unavailable drawdown and win rate.
The API never serializes Infinity or NaN for these cases. Money and accounting
stay Decimal on the backend; chart geometry and indicator series use JavaScript
numbers. Charts do not change fill calculations or execute orders.

## API contracts

All endpoints retain the existing `X-API-Key` authentication and no-store policy.

```text
GET /api/v1/trading/quotes/AAPL/history?period=2y
GET /api/v1/trading/portfolio/metrics
```

History accepts `period=1y` or `2y` and returns daily OHLCV under `candles`, plus
ticker, currency, source, adjustment method, fetched timestamp and dropped-row
count. Each candle contains `time` in YYYY-MM-DD format, `open`, `high`, `low`,
`close`, and nonnegative integer `volume`. The provider request is bounded to
600 daily rows. Results are sorted and deduplicated by session date. Invalid,
nonfinite or inconsistent OHLCV rows are excluded and counted. Empty or unusable
history returns 503. Invalid period returns 422, and offline/disallowed-symbol
requests return 403 before provider access.

The portfolio metrics response adds an `analytics` object with method,
coverage_complete, completed_exits, wins, losses, breakeven, win_loss_ratio,
win_rate_percent, max_drawdown_percent and initial_capital. Existing fields keep
their behavior, including unavailable market valuations while offline.

## Test and build

From `frontend/`, run the following commands. Browser tests use the same
headless Edge setup on Windows as Phase 6, and synthetic HTTP fixtures.

```powershell
npm run typecheck
npm run test:e2e
npm run build
```

From the repository root, use the existing Python environment or Docker runtime.
Set TEST_DATABASE_URL to an initialized disposable PostgreSQL/pgvector database
to include database accounting tests. Never point it at personal/demo ledger data.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Focused tests include `tests/test_market_history.py` and
`tests/test_portfolio_analytics.py`. The browser suite covers selection from both
positions and cards, adding/removing symbols, provider failures, offline policy,
indicator panes and range controls. Pure indicator tests check rolling SMA and
Wilder RSI initialization, smoothing, flat-price and insufficient-history cases.

## Validation results

The complete backend suite passed with 146 tests using Python 3.11 and a real,
isolated PostgreSQL 16/pgvector database. The frontend suite passed 13 checks,
including four indicator-math checks and nine headless Edge browser tests.
Strict TypeScript validation and the production Next.js build passed. The
production npm dependency audit reported zero known vulnerabilities.

The live demo history endpoint returned 502 valid AAPL candles with zero excluded
rows. A browser smoke test rendered actual AAPL and MSFT histories through the
local API, enabled SMA 200 and RSI 14, and reported no runtime errors. Test-suite
market responses are synthetic fixtures; this additional read-only smoke test
used the provider responses. No test placed orders in the existing demo ledger.
Desktop and mobile chart layouts were visually inspected.
