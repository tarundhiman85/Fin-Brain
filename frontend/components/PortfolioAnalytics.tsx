"use client";

import { quantity, type PortfolioAnalytics as Analytics } from "@/services/api";
export default function PortfolioAnalytics({
  analytics,
}: {
  analytics?: Analytics;
}) {
  const available = analytics?.coverage_complete;
  const ratio =
    analytics?.win_loss_ratio != null
      ? `${quantity(analytics.win_loss_ratio)} : 1`
      : available && analytics!.wins! > 0 && analytics!.losses === 0
        ? "No losing exits"
        : "—";
  return (
    <section
      className="panel analytics-panel"
      aria-label="Paper trade analytics"
    >
      <div className="panel-heading">
        <div>
          <h2>Paper trade analytics</h2>
          <p>Settled sell fills · average-cost accounting</p>
        </div>
        <span className="subtle-label">REALIZED RESULTS</span>
      </div>
      <div className="analytics-grid">
        <div>
          <span>Winning / losing exits</span>
          <strong>
            {available ? `${analytics.wins} / ${analytics.losses}` : "—"}
          </strong>
          <small>
            {available
              ? `${analytics.breakeven} breakeven · ${analytics.completed_exits} completed sells`
              : "Awaiting a complete ledger"}
          </small>
        </div>
        <div>
          <span>Win / loss ratio</span>
          <strong>{ratio}</strong>
          <small>Count of winning exits ÷ losing exits</small>
        </div>
        <div>
          <span>Win rate</span>
          <strong>
            {analytics?.win_rate_percent != null
              ? `${quantity(analytics.win_rate_percent)}%`
              : "—"}
          </strong>
          <small>Breakeven exits excluded</small>
        </div>
        <div>
          <span>Max drawdown · realized capital</span>
          <strong>
            {analytics?.max_drawdown_percent != null
              ? `${quantity(analytics.max_drawdown_percent)}%`
              : "—"}
          </strong>
          <small>Peak-to-trough capital decline at sell fills</small>
        </div>
      </div>
      <p className="analytics-method">
        Drawdown follows inferred starting capital plus cumulative realized
        P&amp;L. Open-position price changes are excluded.
        {analytics && !available
          ? " Ledger coverage or starting capital is insufficient for these statistics."
          : ""}
      </p>
    </section>
  );
}
