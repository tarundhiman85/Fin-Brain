import { test, expect, type Page, type Route } from "@playwright/test";

const instant = "2025-01-31T16:00:00.123456+00:00";
const step = {
  call_id: "search-1",
  tool: "search_financial_kb",
  arguments: { query: "dining" },
  started_at: instant,
  completed_at: instant,
  status: "succeeded",
  observation: {
    ok: true,
    data: {
      results: [
        {
          id: "chunk-fixture",
          content: "Fictional dining spending USD 100.00",
          metadata: { source: "fictional-statement.csv" },
        },
      ],
    },
  },
};
function response(
  body: Record<string, string>,
  answer = "Dining spending was **USD 100.00** based on chunk-fixture.",
) {
  return {
    ...body,
    answer,
    status: "completed",
    steps: [step],
    started_at: instant,
    completed_at: instant,
    routing: {
      engine: "local",
      provider: "ollama",
      model: "fixture-model",
      reason: "private_context",
    },
    routing_events: [
      {
        engine: "local",
        provider: "ollama",
        model: "fixture-model",
        duration_ms: 125,
        status: "ok",
        started_at: instant,
      },
    ],
    replayed: false,
  };
}
async function fulfill(route: Route, value: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    headers: { "Access-Control-Allow-Origin": "http://127.0.0.1:3000" },
    body: JSON.stringify(value),
  });
}
async function mockApi(page: Page, chat?: (route: Route) => Promise<void>) {
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") {
      await route.fulfill({
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": "http://127.0.0.1:3000",
          "Access-Control-Allow-Headers": "x-api-key,content-type",
          "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
      });
      return;
    }
    if (path.endsWith("/llm/status"))
      return fulfill(route, {
        offline_mode: false,
        cloud_enabled: false,
        cloud_configured: false,
        public_tickers: ["AAPL", "MSFT", "NVDA"],
        default_mode: "auto",
      });
    if (path.endsWith("/agent/chat")) {
      if (chat) return chat(route);
      await new Promise((resolve) => setTimeout(resolve, 250));
      return fulfill(route, response(request.postDataJSON()));
    }
    if (path.endsWith("/events"))
      return route.fulfill({
        contentType: "text/event-stream",
        headers: { "Access-Control-Allow-Origin": "http://127.0.0.1:3000" },
        body: `: heartbeat\n\nevent: progress\ndata: ${JSON.stringify({ steps: [step], routing: null, routing_events: [], response: null })}\n\n`,
      });
    if (path.endsWith("/portfolio/metrics"))
      return fulfill(route, {
        id: 1,
        currency: "USD",
        cash_balance: "90000.00",
        equity: "101000.00",
        realized_pnl: "250.00",
        unrealized_pnl: "1000.00",
        total_pnl: "1250.00",
        holdings_cost_basis: "10000.00",
        market_value: "11000.00",
        ledger_coverage_complete: true,
        valuation_complete: true,
        snapshot_at: instant,
        updated_at: instant,
        holdings: [
          {
            ticker: "AAPL",
            shares: "50.00000000",
            buy_price: "200.00000000",
            cost_basis: "10000.00",
            market_price: "220.00",
            market_value: "11000.00",
            unrealized_pnl: "1000.00",
            quote_as_of: instant,
            quote_stale: false,
            quote_error: null,
          },
        ],
      });
    if (path.endsWith("/trading/orders"))
      return fulfill(route, {
        currency: "USD",
        next_before_id: null,
        orders: [
          {
            id: 42,
            ticker: "AAPL",
            shares: "50.00000000",
            fill_price: "200.00000000",
            order_type: "BUY",
            status: "CLOSED",
            timestamp: instant,
            settled_notional: "10000.00",
            quote_as_of: instant,
            request_id: "fixture-receipt",
          },
        ],
      });
    if (path.includes("/market/quote/"))
      return fulfill(route, {
        ticker: path.split("/").at(-1),
        price: "220.00",
        currency: "USD",
        as_of: instant,
        fetched_at: instant,
        age_seconds: 2000,
        session_open: "215.00",
        session_low: "213.00",
        session_high: "222.00",
        session_volume: 1000,
        source: "yahoo_finance",
        price_basis: "regular_session_1m_close",
      });
    return fulfill(route, { detail: "Unexpected fixture route" }, 404);
  });
}
async function connect(page: Page) {
  await page.goto("/");
  await page.getByLabel("Backend API key").fill("fictional-browser-test-key");
  await page.getByRole("button", { name: "Connect securely" }).click();
  await expect(
    page.getByText("Backend connected", { exact: true }),
  ).toBeVisible();
}
async function send(page: Page, message = "Summarize dining") {
  await page.getByLabel("Ask FinBrain").fill(message);
  await page.getByRole("button", { name: "Send message", exact: true }).click();
}

test("chat sources and latency survive dashboard navigation", async ({
  page,
}) => {
  await mockApi(page);
  await connect(page);
  await send(page);
  await expect(
    page.getByText("Dining spending was", { exact: false }),
  ).toBeVisible();
  await page.locator(".sources > summary").click();
  await expect(
    page.getByText("Fictional dining spending USD 100.00", { exact: true }),
  ).toBeVisible();
  await page.locator(".tool-trace > summary").click();
  await expect(page.getByText("Inference latency · 1 calls")).toBeVisible();
  await page.getByRole("link", { name: "Trading dashboard" }).click();
  await expect(page.getByText("$101,000.00", { exact: true })).toBeVisible();
  await expect(
    page.getByText("2025-01-31 16:00:00.123456Z", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText("Settled", { exact: true })).toBeVisible();
  await page.screenshot({
    path: "test-results/dashboard-desktop.png",
    fullPage: true,
  });
  await page.getByRole("link", { name: "Chat assistant" }).click();
  await expect(
    page.getByText("Dining spending was", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("Backend connected", { exact: true }),
  ).toBeVisible();
});

test("uncertain network failure retries the identical paper request", async ({
  page,
}) => {
  const bodies: Record<string, string>[] = [];
  await mockApi(page, async (route) => {
    const body = route.request().postDataJSON();
    bodies.push(body);
    if (bodies.length === 1) return route.abort("failed");
    return fulfill(route, response(body));
  });
  await connect(page);
  await send(page, "Check my virtual cash");
  await expect(
    page.getByRole("button", { name: "Resume request", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Resume request", exact: true })
    .click();
  await expect(
    page.getByText("Dining spending was", { exact: false }),
  ).toBeVisible();
  expect(bodies).toHaveLength(2);
  expect(bodies[0]).toEqual(bodies[1]);
});

test("untrusted markdown cannot load external images or links", async ({
  page,
}) => {
  const external: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("exfil.example")) external.push(request.url());
  });
  await mockApi(page, async (route) =>
    fulfill(
      route,
      response(
        route.request().postDataJSON(),
        "Safe answer [untrusted link](https://exfil.example/secret) ![pixel](https://exfil.example/pixel) <script>alert('x')</script>",
      ),
    ),
  );
  await connect(page);
  await send(page);
  await expect(page.getByText("Safe answer", { exact: false })).toBeVisible();
  await expect(
    page.locator(".markdown a, .markdown img, .markdown script"),
  ).toHaveCount(0);
  expect(external).toEqual([]);
});

test("mobile layouts retain navigation and avoid horizontal page overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await connect(page);
  await expect(
    page.getByRole("heading", { name: "Make sense of your money." }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/chat-mobile.png",
    fullPage: true,
  });
  await page.getByRole("link", { name: "Trading dashboard" }).click();
  await expect(page.getByText("$101,000.00", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/dashboard-mobile.png",
    fullPage: true,
  });
});

test("welcome screen and disconnected state are honest", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Send message", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Cloud only", exact: true }),
  ).toBeDisabled();
  await page.screenshot({
    path: "test-results/chat-desktop.png",
    fullPage: true,
  });
});

test("financial formatting preserves large cents and tiny decimal values", async ({
  page,
}) => {
  await mockApi(page);
  await page.route(
    "http://127.0.0.1:8000/api/v1/trading/portfolio/metrics",
    (route) =>
      fulfill(route, {
        id: 1,
        currency: "USD",
        cash_balance: "99999999999999999.99",
        equity: "100000000000000000.01",
        realized_pnl: "-1.01",
        unrealized_pnl: "1E-8",
        total_pnl: "-1.01",
        holdings_cost_basis: "0.00",
        market_value: "0.02",
        ledger_coverage_complete: true,
        valuation_complete: true,
        snapshot_at: instant,
        updated_at: instant,
        holdings: [],
      }),
  );
  await connect(page);
  await page.getByRole("link", { name: "Trading dashboard" }).click();
  await expect(
    page.getByText("$100,000,000,000,000,000.01", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("$99,999,999,999,999,999.99", { exact: true }),
  ).toBeVisible();
  await expect(
    page.locator(".metric-card").nth(2).getByText("$0.00", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("−$1.01", { exact: true })).toBeVisible();
});
