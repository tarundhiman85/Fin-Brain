# Phase 6 chat UI and paper trading dashboard

## What runs locally

`frontend/` is a Next.js App Router application using React, TypeScript and
Tailwind CSS. It connects directly to the authenticated FastAPI API. The API key
is entered through the sidebar and retained in browser memory. No key belongs in
a `NEXT_PUBLIC_*` environment variable. Chat answers, financial context and model
credentials are never sent through a Next.js proxy or an external UI service.

The chat view includes Auto, Local Only and Cloud Only routing, markdown answers,
live checkpoint-based tool traces, per-tool and inference latency, collapsible
source text/metadata, request identifiers and retry recovery. Responses and
conversation state survive navigation between the two views within the same tab.

The dashboard shows portfolio equity, virtual cash, realized/unrealized P&L,
active holdings, timestamped delayed quote cards and keyset-paginated order logs.
Unknown valuations remain unavailable. Balances use the backend's decimal strings
and a BigInt-based formatter to preserve cents for large balances. Prices are
rounded to two decimals for display with full fill prices in their hover text.
Order timestamps retain the fractional precision returned by PostgreSQL and are
displayed in UTC.

## 1. Prepare the backend

Use Python 3.11+, PostgreSQL/pgvector and the existing Phase 1–4 configuration.
From the repository root, activate your Python environment and install dependencies.

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Set `DATABASE_URL`, `API_KEY`, `OLLAMA_URL` and `LLM_LOCAL_MODEL` in your backend
`.env` or process environment. `API_KEY` must be at least 32 characters.
Initialize an empty database and apply the existing additive upgrades.

```powershell
python database.py
python upgrade_phase2.py
python upgrade_phase3.py
```

For an already initialized database, retain its existing embedding model identity.
Refer to the Phase 1 guide for statement ingestion and Phase 4 for Ollama model
pulls and context configuration. No Phase 6 schema migration is required.

Add the exact browser origins to the backend configuration as a JSON array.
These are the defaults and require no override when using port 3000 locally.

```dotenv
UI_ORIGINS=["http://localhost:3000","http://127.0.0.1:3000"]
```

Origins contain scheme, hostname and port, without paths or trailing slashes.
Wildcards and credential-bearing origins are rejected. CORS permits GET/POST with
`Content-Type` and `X-API-Key`; preflights do not bypass API authentication.
Financial responses use `Cache-Control: no-store`.

Start the API on loopback.

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
```

## 2. Install and start the frontend

Use Node.js 22+ or 24 LTS and npm. Next.js requires at least Node.js 20.9.
The package lock pins the installed dependency tree. [Next.js installation](https://nextjs.org/docs/app/getting-started/installation)

In a second terminal, run from the repository root.

```powershell
cd frontend
Copy-Item .env.example .env.local
npm ci
npm run dev
```

Open `http://127.0.0.1:3000`. The development and production scripts bind to
loopback. The Next.js environment template contains only public configuration.

```dotenv
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
NEXT_TELEMETRY_DISABLED=1
```

The Next.js configuration also disables telemetry. The UI uses system fonts and
bundled icons. External images in model markdown are disabled, raw HTML is
ignored, and markdown links render as text to avoid unsolicited remote requests.

If ports change, update `NEXT_PUBLIC_API_BASE_URL` and the corresponding backend
`UI_ORIGINS`, then restart both processes. Public Next.js environment variables
are captured in the frontend build, so rebuild production assets after changing
the API URL. `localhost` and `127.0.0.1` are distinct CORS origins.

Paste the backend `API_KEY` into the sidebar and select **Connect securely**.
The connection check calls the authenticated `/api/v1/llm/status` endpoint.
No financial values are shown as demo data in the application. Empty and
unavailable states reflect the backend response.

## 3. Use chat and inspect execution

Choose a routing mode and type a query. Enter submits and Shift+Enter inserts a
newline. Suggestion buttons populate a draft without submitting it.

Sample requests include the following.

- Find my recent dining transactions and cite the financial sources.
- Check my virtual cash and paper holdings without executing any trades.
- Check Apple stock price, verify my virtual cash balance, and buy 10 shares if I have enough paper money.

Sending the last example authorizes a simulated trade. Paper execution remains
subject to the backend's balance, position, price and privacy policies. The UI
uses the existing agent endpoint and ledger receipts. It does not implement a
separate client-side fill engine.

Auto and Local Only preserve private financial inference on Ollama. Local Only
can still use Yahoo quotes when the requested task permits market access.
`LLM_OFFLINE_MODE=true` disables external quotes and new paper fills. Cloud Only
is disabled in the UI when cloud credentials/capability are absent or the server
is offline. Private requests remain blocked from cloud inference even when the
user selects Cloud Only. Reconnect after changing backend routing settings to
refresh the displayed capabilities.

Open **Tool activity** to inspect arguments, observations, statuses and elapsed
latency. Open **Financial sources** for exact chunk IDs, text and source metadata.
The UI displays saved tool checkpoints and completed public model telemetry.
Internal chain-of-thought is not exposed.

A chat request POST runs concurrently with a read-only SSE subscription. The SSE
endpoint reads committed checkpoints once per second, so very fast calls may
appear already completed. It never starts an agent or places an order. Final
answer text is delivered when synthesis completes, rather than token-by-token.
The complete POST response remains authoritative if live progress is interrupted.

### Request recovery

The browser assigns a UUID before each request. Network/5xx failures keep that
request unresolved, lock new submissions and offer **Resume request** with the
identical message, conversation ID, request UUID and routing options. The backend
can replay a completed result or resume its checkpoint. A 409 conflict remains
visible without silently submitting a new order. Server-reported `failed` and
`limit_reached` results are shown with their recorded trace.

Use **Request details** to record identifiers before leaving an unresolved turn.
Tab memory retains the key, conversation and in-flight request during client-side
navigation. Reloading or closing the tab clears that memory. A before-unload
handler asks the browser to protect unresolved requests, subject to browser policy.
If you deliberately leave anyway, use the recorded identifiers to recover through
the API before submitting the same trade as a new request.

The backend's completed result can be recovered with the original POST body.

```powershell
$headers = @{ 'X-API-Key' = $env:API_KEY }
$body = @{
  message = '<original message>'
  conversation_id = '<original conversation ID>'
  request_id = '<original request UUID>'
  routing_mode = 'auto'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/agent/chat -Headers $headers -ContentType application/json -Body $body
```

Use the original routing mode too. Recovery shares the existing Phase 3
idempotency semantics, including stable per-request order receipts.

## 4. Inspect the dashboard

The portfolio, latest orders and watchlist quote cards refresh every 30 seconds
while their view is visible. Each poll waits for its prior request to finish.
Errors preserve the last snapshot with an explicit stale-data message. Portfolio
refresh is also available manually. Market cards show regular-session prices,
quote timestamps, session ranges, and change from session open. They do not
fabricate a historical performance chart or imply exchange-real-time execution.
Yahoo responses can be delayed, old, unavailable or rate-limited.

P&L calculations reuse the backend's existing average-cost portfolio service.
Equity is virtual cash plus marked holdings. Incomplete valuation or legacy
ledger coverage produces unavailable values with context. Holdings show quote
age and timestamps. A portfolio with no open holdings can still have valid cash
and equity while offline.

The order ledger reads persisted orders from the configured
`PAPER_PORTFOLIO_ID`, includes both BUY and SELL, and loads older rows using a
stable ID cursor. `CLOSED` displays as Settled and `OPEN` displays as Open. A
legacy row without an execution receipt shows its settlement amount as
unavailable. The ledger is shared with existing MCP/API callers. Receipt request
IDs identify ledger executions and are distinct from parent chat request UUIDs.

Add watchlist symbols only from `LLM_PUBLIC_TICKERS`. Up to six watchlist cards are
supported. The default watchlist shows enabled symbols among AAPL, MSFT and NVDA.
Removing a card changes the current UI session only. No dashboard action itself
submits a BUY or SELL order.

## 5. API additions

`main_ui_bridge.py` mounts under the existing application-wide API-key dependency.
The original endpoints retain their behavior.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/agent/chat` | Existing resumable agent execution and final JSON answer |
| `GET /api/v1/agent/requests/{request_id}/events?conversation_id=...` | Authenticated SSE checkpoint snapshots and saved final result |
| `GET /api/v1/trading/portfolio` | Existing raw cash/holdings snapshot |
| `GET /api/v1/trading/portfolio/metrics` | Configured portfolio P&L and indicative valuations |
| `GET /api/v1/trading/orders?limit=50&before_id=...` | Portfolio-scoped order history with exact decimal strings |
| `GET /api/v1/market/quote/{ticker}` | Allowlisted, timestamped quote respecting offline mode |

The browser uses fetch-based SSE so the API key stays in a request header.
Credentials do not appear in event-stream URLs. Stream errors have explicit SSE
error frames. Streams stop on client disconnect, completion, unavailable state or
a 15-minute observation timeout. Disconnecting a progress stream does not cancel
the separate POST or roll back a confirmed trade.

For a reverse proxy, disable buffering for SSE and allow sufficiently long request
timeouts. All protected reads remain subject to authentication. The application
is a single-owner workspace and uses the existing shared owner key.

## 6. Build and verify

From `frontend/`, run the checks and production server.

```powershell
npm run typecheck
npm run build
npm start
```

Browser regression tests use mocked API responses and inspect real UI behavior.
On Windows, they use installed Microsoft Edge. On Linux/macOS, install Playwright's
Chromium first.

```powershell
# Linux/macOS browser setup only
npx playwright install chromium
npm run test:e2e
```

Run the backend suite from the repository root.

```powershell
python -m pytest -q
```

Set `TEST_DATABASE_URL` to an initialized disposable PostgreSQL/pgvector database
to include order pagination, checkpoint persistence and other database tests.
`frontend/node_modules`, build output, local environment files and browser artifacts
are ignored by Git. The backend Docker build excludes the separate frontend tree.

### Validation results

The production Next.js build and strict TypeScript checks passed. Six headless
Edge browser tests passed, covering desktop/mobile layouts, source citations,
model latency, navigation without session loss, identical-UUID recovery and
untrusted markdown. Screenshots were inspected at desktop and mobile sizes.
Browser market prices and agent responses in these tests are synthetic fixtures.
All 133 backend tests passed using Python 3.11 and real PostgreSQL/pgvector,
with mocked market/model calls. The production npm dependency audit reported
zero known vulnerabilities.
Native browser automation could not start in the Windows sandbox, so browser
verification used headless Edge through Playwright.

Advanced charts, technical indicators and realized-trade analytics are documented in [README_AdvancedDashboard.md](README_AdvancedDashboard.md).
