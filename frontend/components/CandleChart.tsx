"use client";

import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  LineStyle,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
} from "lightweight-charts";
import { RefreshCw } from "lucide-react";
import TechnicalIndicators, {
  type IndicatorOptions,
} from "@/components/TechnicalIndicators";
import {
  api,
  errorMessage,
  money,
  timestamp,
  type Candle,
  type CandleHistory,
} from "@/services/api";
import {
  simpleMovingAverage,
  relativeStrengthIndex,
} from "@/services/indicators";

type ChartState = {
  chart: IChartApi;
  sma50: ISeriesApi<"Line">;
  sma200: ISeriesApi<"Line">;
  rsi?: ISeriesApi<"Line">;
};
type Range = "3mo" | "6mo" | "1y" | "2y";

export default function CandleChart({
  ticker,
  apiKey,
  offline = false,
}: {
  ticker: string;
  apiKey: string;
  offline?: boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chartState = useRef<ChartState | null>(null);
  const [history, setHistory] = useState<CandleHistory | null>(null);
  const [hovered, setHovered] = useState<Candle | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [range, setRange] = useState<Range>("6mo");
  const [indicators, setIndicators] = useState<IndicatorOptions>({
    sma50: true,
    sma200: false,
    rsi: false,
  });
  const current =
    history?.ticker === ticker && !offline && apiKey ? history : null;

  useEffect(() => {
    setHistory(null);
    setHovered(null);
    setError("");
    if (!apiKey || offline) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    api<CandleHistory>(
      `/api/v1/trading/quotes/${encodeURIComponent(ticker)}/history?period=2y`,
      apiKey,
      { signal: controller.signal },
    )
      .then((result) => {
        if (!controller.signal.aborted) {
          if (result.ticker !== ticker || !result.candles.length)
            throw new Error("History contains no candles for this ticker");
          setHistory(result);
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [ticker, apiKey, offline, revision]);

  useEffect(() => {
    if (!current || !container.current) return;
    const chart = createChart(container.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#fffefa" },
        textColor: "#65755c",
        attributionLogo: true,
        panes: { separatorColor: "#dfe6d4", separatorHoverColor: "#bfceaa" },
      },
      grid: {
        vertLines: { color: "#f0f3e9" },
        horzLines: { color: "#edf1e3" },
      },
      rightPriceScale: { borderColor: "#e2e8d7" },
      timeScale: { borderColor: "#e2e8d7", timeVisible: false, rightOffset: 3 },
    });
    try {
      const candles = chart.addSeries(CandlestickSeries, {
        upColor: "#628b69",
        downColor: "#ba7860",
        borderVisible: false,
        wickUpColor: "#628b69",
        wickDownColor: "#ba7860",
        priceFormat: { type: "price", precision: 2, minMove: 0.01 },
      });
      candles.setData(
        current.candles.map(({ time, open, high, low, close }) => ({
          time,
          open,
          high,
          low,
          close,
        })),
      );
      const volume = chart.addSeries(
        HistogramSeries,
        {
          priceFormat: { type: "volume" },
          priceLineVisible: false,
          lastValueVisible: false,
        },
        1,
      );
      volume.setData(
        current.candles.map((bar) => ({
          time: bar.time,
          value: bar.volume,
          color: bar.close >= bar.open ? "#8aaa8599" : "#c99a8599",
        })),
      );
      chart.panes()[0].setStretchFactor(4);
      chart.panes()[1].setStretchFactor(1);
      const sma50 = chart.addSeries(LineSeries, {
        color: "#b88c38",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      const sma200 = chart.addSeries(LineSeries, {
        color: "#6589b2",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      sma50.setData(simpleMovingAverage(current.candles, 50));
      sma200.setData(simpleMovingAverage(current.candles, 200));
      chartState.current = { chart, sma50, sma200 };
      chart.subscribeCrosshairMove((point) => {
        const index = point.logical == null ? -1 : Math.round(point.logical);
        setHovered(
          point.time != null && index >= 0 && index < current.candles.length
            ? current.candles[index]
            : null,
        );
      });
      chart.timeScale().fitContent();
    } catch {
      setError("The provider history could not be rendered");
    }
    return () => {
      chartState.current = null;
      chart.remove();
    };
  }, [current]);

  useEffect(() => {
    const state = chartState.current;
    if (!state || !current) return;
    state.sma50.applyOptions({ visible: indicators.sma50 });
    state.sma200.applyOptions({ visible: indicators.sma200 });
    if (indicators.rsi && !state.rsi) {
      state.rsi = state.chart.addSeries(
        LineSeries,
        {
          color: "#827599",
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: true,
          autoscaleInfoProvider: () => ({
            priceRange: { minValue: 0, maxValue: 100 },
          }),
        },
        2,
      );
      state.rsi
        .priceScale()
        .applyOptions({ scaleMargins: { top: 0, bottom: 0 } });
      state.rsi.setData(relativeStrengthIndex(current.candles));
      for (const price of [30, 70])
        state.rsi.createPriceLine({
          price,
          color: "#b7adbf",
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
        });
      state.chart.panes()[2].setStretchFactor(1.3);
    } else if (!indicators.rsi && state.rsi) {
      state.chart.removeSeries(state.rsi);
      state.rsi = undefined;
    }
  }, [indicators, current]);

  useEffect(() => {
    if (!current || !chartState.current) return;
    const end = new Date(`${current.candles.at(-1)!.time}T00:00:00Z`);
    const months = { "3mo": 3, "6mo": 6, "1y": 12, "2y": 24 }[range];
    end.setUTCMonth(end.getUTCMonth() - months);
    const cutoff = end.toISOString().slice(0, 10);
    const start = current.candles.findIndex((bar) => bar.time >= cutoff);
    chartState.current.chart
      .timeScale()
      .setVisibleLogicalRange({
        from: Math.max(0, start),
        to: current.candles.length + 2,
      });
  }, [range, current]);

  const bar = hovered || current?.candles.at(-1);
  return (
    <section
      className="panel candle-panel"
      aria-label={`${ticker} historical chart`}
    >
      <div className="panel-heading">
        <div>
          <span className="eyebrow">PRICE & VOLUME</span>
          <h2>
            {ticker}{" "}
            <span className="chart-currency">
              {current?.currency || ""} · Daily candles
            </span>
          </h2>
          <p>Drag to pan, scroll to zoom, and hover for OHLCV values.</p>
        </div>
        <button
          className="icon-button"
          aria-label={`Refresh ${ticker} history`}
          disabled={loading || offline || !apiKey}
          onClick={() => setRevision((value) => value + 1)}
        >
          <RefreshCw size={16} className={loading ? "spin" : ""} />
        </button>
      </div>
      <div className="chart-toolbar">
        <TechnicalIndicators
          value={indicators}
          onChange={setIndicators}
          disabled={!current}
        />
        <div
          className="range-switch"
          role="group"
          aria-label="Chart date range"
        >
          {(["3mo", "6mo", "1y", "2y"] as const).map((value) => (
            <button
              key={value}
              aria-pressed={range === value}
              disabled={!current}
              onClick={() => setRange(value)}
            >
              {value.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
      {bar && (
        <div className="candle-legend" aria-live="off">
          <strong>{bar.time}</strong>
          {(
            [
              ["O", bar.open],
              ["H", bar.high],
              ["L", bar.low],
              ["C", bar.close],
            ] as const
          ).map(([label, value]) => (
            <span key={label}>
              {label} <b>{money(String(value), current?.currency)}</b>
            </span>
          ))}
          <span>
            Volume <b>{bar.volume.toLocaleString()}</b>
          </span>
        </div>
      )}
      {error && (
        <p className="error-banner" role="alert">
          {error}
        </p>
      )}
      {!current && (
        <div className="empty-state">
          <h3>
            {offline
              ? "Market history is offline"
              : loading
                ? `Loading ${ticker} history…`
                : "History unavailable"}
          </h3>
          <p>
            {offline
              ? "Enable market access on the backend to load candles."
              : "Historical candles load from your authenticated market API."}
          </p>
        </div>
      )}
      <div
        ref={container}
        className={`chart-canvas ${indicators.rsi ? "with-rsi" : ""}`}
        hidden={!current}
        data-testid="candle-canvas"
        role="img"
        aria-label={`${ticker} candlesticks, volume${indicators.rsi ? " and RSI 14" : ""}`}
      />
      {current && (
        <>
          <div className="chart-pane-labels">
            <span>Price · {current.currency}</span>
            <span>Volume</span>
            {indicators.rsi && <span>RSI 14 · Wilder · 30 / 70</span>}
          </div>
          {indicators.sma200 && current.candles.length < 200 && (
            <p className="inline-error">
              SMA 200 needs 200 complete trading bars; this symbol has fewer.
            </p>
          )}
          {current.dropped_rows > 0 && (
            <p className="inline-error">
              {current.dropped_rows} invalid provider rows were excluded.
            </p>
          )}
          <details className="chart-data-table">
            <summary>View OHLCV data · latest 30 sessions</summary>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Session date</th>
                    <th>Open</th>
                    <th>High</th>
                    <th>Low</th>
                    <th>Close</th>
                    <th>Volume</th>
                  </tr>
                </thead>
                <tbody>
                  {current.candles
                    .slice(-30)
                    .reverse()
                    .map((point) => (
                      <tr key={point.time}>
                        <td>{point.time}</td>
                        <td>{money(String(point.open), current.currency)}</td>
                        <td>{money(String(point.high), current.currency)}</td>
                        <td>{money(String(point.low), current.currency)}</td>
                        <td>{money(String(point.close), current.currency)}</td>
                        <td>{point.volume.toLocaleString()}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </details>
          <div className="table-footer">
            <span>
              Yahoo Finance · fetched {timestamp(current.fetched_at)} ·
              Current-session candle may change.
            </span>
            <a
              href="https://www.tradingview.com/"
              target="_blank"
              rel="noopener noreferrer"
            >
              TradingView Lightweight Charts™ · Copyright (с) 2025 TradingView,
              Inc.
            </a>
          </div>
        </>
      )}
    </section>
  );
}
