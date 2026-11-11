"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowDownLeft,
  ArrowRight,
  ArrowUpRight,
  ChartNoAxesCombined,
  CircleAlert,
  Clock3,
  Coins,
  RefreshCw,
  Wallet,
} from "lucide-react";
import CandleChart from "@/components/CandleChart";
import PortfolioAnalytics from "@/components/PortfolioAnalytics";
import { useSession } from "@/components/SessionProvider";
import {
  api,
  errorMessage,
  money,
  quantity,
  timestamp,
  type Order,
  type OrderPage,
  type Portfolio,
  type Quote,
} from "@/services/api";

function usePolling<T>(path: string, key: string, enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    if (!key || !enabled) {
      setData(null);
      setError("");
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      if (!document.hidden) {
        setRefreshing(true);
        try {
          const next = await api<T>(path, key, { signal: controller.signal });
          if (!controller.signal.aborted) {
            setData(next);
            setError("");
          }
        } catch (cause) {
          if (!controller.signal.aborted) setError(errorMessage(cause));
        } finally {
          if (!controller.signal.aborted) setRefreshing(false);
        }
      }
      if (!controller.signal.aborted) timer = setTimeout(load, 30000);
    }
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, key, enabled, revision]);
  return { data, error, refreshing, refresh };
}

function MetricCard({
  label,
  value,
  note,
  icon: Icon,
  prominent = false,
  pnl = false,
  currency = "USD",
}: {
  label: string;
  value: string | null | undefined;
  note: string;
  icon: typeof Wallet;
  prominent?: boolean;
  pnl?: boolean;
  currency?: string;
}) {
  const negative = value?.startsWith("-");
  return (
    <article className={`metric-card ${prominent ? "metric-primary" : ""}`}>
      <div className="metric-label">
        {label}
        <Icon size={17} />
      </div>
      <p
        className={
          pnl && value != null ? (negative ? "negative" : "positive") : ""
        }
      >
        {money(value, currency)}
      </p>
      <small>{note}</small>
    </article>
  );
}
function MarketCard({
  ticker,
  apiKey,
  onSelect,
}: {
  ticker: string;
  apiKey: string;
  onSelect: (ticker: string) => void;
}) {
  const { data, error, refreshing, refresh } = usePolling<Quote>(
    `/api/v1/market/quote/${encodeURIComponent(ticker)}`,
    apiKey,
  );
  const change =
    data && Number(data.session_open) > 0
      ? (Number(data.price) / Number(data.session_open) - 1) * 100
      : null;
  return (
    <article className="market-card">
      <div className="market-card-top">
        <span className="ticker-mark">{ticker.slice(0, 1)}</span>
        <div>
          <button
            className="ticker-button"
            aria-label={`View ${ticker} market chart`}
            onClick={() => onSelect(ticker)}
          >
            {ticker}
          </button>
          <small>Yahoo Finance</small>
        </div>
        <button
          aria-label={`Refresh ${ticker} quote`}
          className="icon-button"
          onClick={refresh}
          disabled={refreshing}
        >
          <RefreshCw size={14} className={refreshing ? "spin" : ""} />
        </button>
      </div>
      <div className="market-price">
        <strong>{money(data?.price, data?.currency)}</strong>
        {change != null && (
          <span className={change < 0 ? "negative" : "positive"}>
            {change >= 0 ? "+" : ""}
            {change.toFixed(2)}% <small>from session open</small>
          </span>
        )}
      </div>
      {data ? (
        <>
          <div className="quote-range">
            <span>
              Low <b>{money(data.session_low, data.currency)}</b>
            </span>
            <span>
              High <b>{money(data.session_high, data.currency)}</b>
            </span>
          </div>
          <p className="quote-time">
            <Clock3 size={12} /> {timestamp(data.as_of)}
          </p>
          <small className="quote-disclaimer">
            {data.age_seconds > 1200 ? "Older quote · " : ""}Delayed
            regular-session data
          </small>
        </>
      ) : (
        <p className="muted">
          {refreshing ? "Fetching timestamped quote…" : "Quote unavailable"}
        </p>
      )}
      {error && (
        <p className="inline-error">
          {error}
          {data ? " Showing last fetched data." : ""}
        </p>
      )}
    </article>
  );
}
function OrderHistory({ apiKey }: { apiKey: string }) {
  const page = usePolling<OrderPage>("/api/v1/trading/orders?limit=50", apiKey);
  const [older, setOlder] = useState<Order[]>([]);
  const [olderCursor, setOlderCursor] = useState<number | null | undefined>(
    undefined,
  );
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState("");
  useEffect(() => {
    setOlder([]);
    setOlderCursor(undefined);
  }, [apiKey]);
  const next =
    olderCursor === undefined ? page.data?.next_before_id : olderCursor;
  async function loadOlder() {
    if (!next || loadingOlder) return;
    setLoadingOlder(true);
    setOlderError("");
    try {
      const result = await api<OrderPage>(
        `/api/v1/trading/orders?limit=50&before_id=${next}`,
        apiKey,
      );
      setOlder((items) => [...items, ...result.orders]);
      setOlderCursor(result.next_before_id);
    } catch (error) {
      setOlderError(errorMessage(error));
    } finally {
      setLoadingOlder(false);
    }
  }
  const orders = [
    ...new Map(
      [...older, ...(page.data?.orders || [])].map((order) => [
        order.id,
        order,
      ]),
    ).values(),
  ].sort((a, b) => b.id - a.id);
  return (
    <section className="panel orders-panel">
      <div className="panel-heading">
        <div>
          <h2>Order ledger</h2>
          <p>Exact paper fills and their persisted timestamps</p>
        </div>
        <button
          className="icon-button"
          aria-label="Refresh orders"
          onClick={page.refresh}
          disabled={page.refreshing}
        >
          <RefreshCw size={16} className={page.refreshing ? "spin" : ""} />
        </button>
      </div>
      {page.error && (
        <p className="error-banner" role="alert">
          {page.error}
          {orders.length ? " Showing previously fetched orders." : ""}
        </p>
      )}
      {orders.length ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Order / asset</th>
                <th>Action</th>
                <th>Shares</th>
                <th>Price</th>
                <th>Settled amount</th>
                <th>Executed at · UTC</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order) => (
                <tr key={order.id}>
                  <td>
                    <strong>{order.ticker}</strong>
                    <small>#{order.id}</small>
                  </td>
                  <td>
                    <span
                      className={`order-side ${order.order_type === "BUY" ? "buy" : "sell"}`}
                    >
                      {order.order_type === "BUY" ? (
                        <ArrowDownLeft size={12} />
                      ) : (
                        <ArrowUpRight size={12} />
                      )}
                      {order.order_type}
                    </span>
                  </td>
                  <td>{quantity(order.shares)}</td>
                  <td title={order.fill_price}>
                    {money(order.fill_price, page.data?.currency)}
                  </td>
                  <td
                    title={
                      order.settled_notional ||
                      "Legacy order without a saved settlement receipt"
                    }
                  >
                    {money(order.settled_notional, page.data?.currency)}
                  </td>
                  <td>
                    <time dateTime={order.timestamp}>
                      {timestamp(order.timestamp)}
                    </time>
                    {order.quote_as_of && (
                      <small>Quote {timestamp(order.quote_as_of)}</small>
                    )}
                    <details className="order-request">
                      <summary>Receipt request ID</summary>
                      <code>{order.request_id || "Legacy order"}</code>
                    </details>
                  </td>
                  <td>
                    <span className="ledger-status">
                      {order.status === "CLOSED" ? "Settled" : "Open"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state">
          <Clock3 size={26} />
          <h3>
            {page.refreshing
              ? "Loading order history…"
              : "Your ledger starts here"}
          </h3>
          <p>
            Confirmed paper orders will appear with their execution timestamps.
          </p>
        </div>
      )}
      {next && (
        <div className="table-footer">
          <button
            className="secondary-button"
            disabled={loadingOlder}
            onClick={loadOlder}
          >
            {loadingOlder ? "Loading…" : "Load earlier orders"}
            <ArrowRight size={14} />
          </button>
        </div>
      )}
      {olderError && (
        <p className="error-banner" role="alert">
          {olderError}
        </p>
      )}
    </section>
  );
}
export default function TradingDashboard() {
  const session = useSession();
  const portfolio = usePolling<Portfolio>(
    "/api/v1/trading/portfolio/metrics",
    session.key,
  );
  const [watchlist, setWatchlist] = useState(["AAPL", "MSFT", "NVDA"]);
  const [newTicker, setNewTicker] = useState("");
  const [watchError, setWatchError] = useState("");
  const data = portfolio.data;
  const [selectedTicker, setSelectedTicker] = useState("AAPL");
  const [addingTicker, setAddingTicker] = useState(false);
  const chartSection = useRef<HTMLDivElement>(null);
  const addition = useRef<AbortController | null>(null);
  useEffect(() => {
    return () => addition.current?.abort();
  }, [session.key]);
  function selectTicker(ticker: string) {
    setSelectedTicker(ticker);
    chartSection.current?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }
  const chartSymbols = [
    ...new Set([
      ...watchlist,
      ...(data?.holdings.map((holding) => holding.ticker) || []),
      selectedTicker,
    ]),
  ].filter((ticker) => session.status?.public_tickers.includes(ticker));
  const chartTicker = chartSymbols.includes(selectedTicker)
    ? selectedTicker
    : chartSymbols[0];
  async function addTicker() {
    const symbol = newTicker.trim().toUpperCase();
    if (!session.status?.public_tickers.includes(symbol)) {
      setWatchError(
        "Choose a ticker enabled in your backend’s public market scope.",
      );
      return;
    }
    if (watchlist.includes(symbol)) {
      setWatchError("This ticker is already on your watchlist.");
      return;
    }
    if (
      watchlist.filter((ticker) =>
        session.status?.public_tickers.includes(ticker),
      ).length >= 6
    ) {
      setWatchError("The watchlist supports six tickers.");
      return;
    }
    if (addingTicker) return;
    const controller = new AbortController();
    addition.current = controller;
    setAddingTicker(true);
    setWatchError("");
    try {
      const quote = await api<Quote>(
        `/api/v1/market/quote/${encodeURIComponent(symbol)}`,
        session.key,
        { signal: controller.signal },
      );
      if (!controller.signal.aborted) {
        if (quote.ticker !== symbol)
          throw new Error("The quote returned a different ticker");
        setWatchlist((items) => [
          ...new Set([
            ...items.filter((ticker) =>
              session.status?.public_tickers.includes(ticker),
            ),
            symbol,
          ]),
        ]);
        setSelectedTicker(symbol);
        setNewTicker("");
      }
    } catch (cause) {
      if (!controller.signal.aborted) setWatchError(errorMessage(cause));
    } finally {
      setAddingTicker(false);
    }
  }
  return (
    <div className="dashboard-page">
      <div className="section-heading">
        <div>
          <span className="eyebrow">YOUR STRATEGIES, IN PRACTICE</span>
          <h1>Paper trading dashboard</h1>
          <p className="heading-description">
            A clear view of your virtual capital and market activity.
          </p>
        </div>
        <button
          className="secondary-button"
          onClick={portfolio.refresh}
          disabled={!session.key || portfolio.refreshing}
        >
          <RefreshCw size={15} className={portfolio.refreshing ? "spin" : ""} />{" "}
          Refresh portfolio
        </button>
      </div>
      <div className="dashboard-context">
        <span>
          <span className={`status-dot ${session.key ? "online" : ""}`} />{" "}
          {session.key
            ? "Connected paper portfolio"
            : "Backend connection required"}
        </span>
        <span>Refreshes every 30 seconds · Delayed market data</span>
      </div>
      {!session.key && (
        <div className="connection-callout">
          <Wallet size={25} />
          <div>
            <h2>Connect to see your portfolio</h2>
            <p>
              Enter your backend API key in the sidebar to load virtual
              balances, holdings and orders.
            </p>
          </div>
        </div>
      )}
      {portfolio.error && (
        <p className="error-banner" role="alert">
          {portfolio.error}
          {data ? " Displaying the previous snapshot." : ""}
        </p>
      )}
      <div className="metric-grid">
        <MetricCard
          label="Portfolio equity"
          value={data?.equity}
          note={
            data
              ? data.valuation_complete
                ? "Virtual cash + marked holdings"
                : "Some market valuations are unavailable"
              : "Waiting for portfolio data"
          }
          icon={ChartNoAxesCombined}
          currency={data?.currency}
          prominent
        />
        <MetricCard
          label="Virtual cash"
          value={data?.cash_balance}
          note="Available paper buying power"
          icon={Wallet}
          currency={data?.currency}
        />
        <MetricCard
          label="Unrealized P&L"
          value={data?.unrealized_pnl}
          note="Open positions at their latest quotes"
          icon={ArrowUpRight}
          currency={data?.currency}
          pnl
        />
        <MetricCard
          label="Realized P&L"
          value={data?.realized_pnl}
          note={
            data && !data.ledger_coverage_complete
              ? "Historical ledger coverage is incomplete"
              : "Settled gains and losses"
          }
          icon={Coins}
          currency={data?.currency}
          pnl
        />
      </div>
      {data && (
        <div className="snapshot-line">
          <span>Snapshot {timestamp(data.snapshot_at)}</span>
          <span>Ledger updated {timestamp(data.updated_at)}</span>
        </div>
      )}
      {data && (!data.valuation_complete || !data.ledger_coverage_complete) && (
        <p className="warning-banner">
          <CircleAlert size={16} />{" "}
          {session.status?.offline_mode
            ? "Offline mode disables market valuations. "
            : "Some quotes or historical ledger entries are unavailable. "}
          Unknown metrics remain unavailable.
        </p>
      )}
      {session.key && <PortfolioAnalytics analytics={data?.analytics} />}
      <section className="panel holdings-panel">
        <div className="panel-heading">
          <div>
            <h2>
              Active positions{" "}
              <span className="count-badge">{data?.holdings.length || 0}</span>
            </h2>
            <p>Average-cost holdings in your simulated portfolio</p>
          </div>
          <span className="subtle-label">VIRTUAL ASSETS</span>
        </div>
        {data?.holdings.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Asset</th>
                  <th>Shares</th>
                  <th>Average cost</th>
                  <th>Market price</th>
                  <th>Market value</th>
                  <th>Unrealized P&L</th>
                  <th>Quote timestamp · UTC</th>
                </tr>
              </thead>
              <tbody>
                {data.holdings.map((holding) => (
                  <tr key={holding.ticker}>
                    <td>
                      <div className="asset-cell">
                        <span className="asset-icon">
                          {holding.ticker.slice(0, 1)}
                        </span>
                        <button
                          className="ticker-button"
                          aria-label={`View ${holding.ticker} position chart`}
                          disabled={
                            !session.status?.public_tickers.includes(
                              holding.ticker,
                            )
                          }
                          onClick={() => selectTicker(holding.ticker)}
                        >
                          {holding.ticker}
                        </button>
                      </div>
                    </td>
                    <td>{quantity(holding.shares)}</td>
                    <td title={holding.buy_price}>
                      {money(holding.buy_price, data.currency)}
                    </td>
                    <td
                      title={
                        holding.market_price ||
                        holding.quote_error ||
                        "Unavailable"
                      }
                    >
                      {money(holding.market_price, data.currency)}
                    </td>
                    <td>{money(holding.market_value, data.currency)}</td>
                    <td
                      className={
                        holding.unrealized_pnl == null
                          ? ""
                          : holding.unrealized_pnl.startsWith("-")
                            ? "negative"
                            : "positive"
                      }
                    >
                      {money(holding.unrealized_pnl, data.currency)}
                    </td>
                    <td>
                      <time dateTime={holding.quote_as_of || undefined}>
                        {timestamp(holding.quote_as_of)}
                      </time>
                      <small>
                        {holding.quote_stale
                          ? "Older quote"
                          : holding.quote_error ||
                            "Delayed regular-session mark"}
                      </small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-state">
            <ChartNoAxesCombined size={29} />
            <h3>
              {portfolio.refreshing
                ? "Loading your positions…"
                : "Room for your next idea"}
            </h3>
            <p>
              {session.key
                ? "No open simulated positions are available in this snapshot."
                : "Your active holdings will appear after you connect."}
            </p>
          </div>
        )}
        <div className="table-footer">
          <span>
            <Clock3 size={12} /> Quotes are indicative and may be delayed.
          </span>
          <Link href="/">
            Discuss a strategy <ArrowUpRight size={14} />
          </Link>
        </div>
      </section>
      {session.key && (
        <div ref={chartSection} className="chart-section">
          <div className="chart-selector">
            <label htmlFor="chart-ticker">Chart ticker</label>
            <select
              id="chart-ticker"
              value={chartTicker || ""}
              onChange={(event) => setSelectedTicker(event.target.value)}
              disabled={!chartSymbols.length}
            >
              {!chartSymbols.length && (
                <option value="">No approved symbols</option>
              )}
              {chartSymbols.map((ticker) => (
                <option key={ticker} value={ticker}>
                  {ticker}
                </option>
              ))}
            </select>
            <span>
              Choose an asset or select its symbol from a position or market
              card.
            </span>
          </div>
          {chartTicker && (
            <CandleChart
              ticker={chartTicker}
              apiKey={session.key}
              offline={session.status?.offline_mode}
            />
          )}
        </div>
      )}
      <section className="market-section">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">A WINDOW INTO THE MARKET</span>
            <h2>Market watch</h2>
          </div>
          {session.key && !session.status?.offline_mode && (
            <form
              className="watch-form"
              onSubmit={(event) => {
                event.preventDefault();
                void addTicker();
              }}
            >
              <label className="sr-only" htmlFor="ticker">
                Add ticker
              </label>
              <input
                id="ticker"
                disabled={addingTicker}
                placeholder="Add ticker"
                maxLength={20}
                value={newTicker}
                onChange={(event) => setNewTicker(event.target.value)}
              />
              <button
                type="submit"
                className="secondary-button"
                disabled={addingTicker || !newTicker.trim()}
              >
                {addingTicker ? "Checking…" : "Add"}
              </button>
            </form>
          )}
        </div>
        {watchError && (
          <p className="inline-error" role="alert">
            {watchError}
          </p>
        )}
        {session.key && session.status?.offline_mode ? (
          <div className="offline-market">
            <CircleAlert size={18} /> Market access is disabled by your
            backend’s offline setting.
          </div>
        ) : !session.key ? (
          <div className="offline-market">
            Connect your backend to fetch timestamped market quotes.
          </div>
        ) : (
          <div className="market-grid">
            {!watchlist.filter((ticker) =>
              session.status?.public_tickers.includes(ticker),
            ).length && (
              <p className="muted">
                Your watchlist is empty. Add an approved ticker above.
              </p>
            )}
            {watchlist
              .filter((ticker) =>
                session.status?.public_tickers.includes(ticker),
              )
              .map((ticker) => (
                <div key={ticker} className="watch-item">
                  <MarketCard
                    ticker={ticker}
                    apiKey={session.key}
                    onSelect={selectTicker}
                  />
                  <button
                    className="remove-watch"
                    aria-label={`Remove ${ticker} from watchlist`}
                    onClick={() =>
                      setWatchlist((items) =>
                        items.filter((symbol) => symbol !== ticker),
                      )
                    }
                  >
                    Remove
                  </button>
                </div>
              ))}
          </div>
        )}
      </section>
      {session.key && <OrderHistory apiKey={session.key} />}
      <div className="paper-notice">
        <span className="paper-badge">SIMULATION WORKSPACE</span>
        <p>
          Balances, positions and orders in this dashboard use virtual money.
          Market quotes include their source timestamps.
        </p>
      </div>
    </div>
  );
}
