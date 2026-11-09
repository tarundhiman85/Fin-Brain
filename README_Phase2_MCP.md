# Phase 2 — Finance and Market MCP Servers

Two independent local stdio processes expose the existing PostgreSQL services.
The official Python MCP SDK v2 supplies MCPServer, typed tools and stdio
transport. The shared finance_service.py powers both FastAPI and MCP retrieval.
Asynchronous tool handlers run existing SQLAlchemy, Ollama and yfinance calls in
a bounded AnyIO worker pool with four workers per process. Each worker owns its
database session. MCP lifecycle shutdown disposes its database pool.

## 1. Install dependencies

From the finbrain repository root, using the Phase 1 virtual environment:

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
~~~

For a new checkout, first create the environment with py -m venv .venv.
On macOS/Linux use python3 and .venv/bin/python in the equivalent commands.
requirements.txt constrains mcp[cli] to >=2.3,<3 and yfinance to >=1.8,<2.
MCP v2 uses mcp.server.mcpserver.MCPServer and snake_case protocol model
attributes. Install the declared dependencies together to get compatible APIs.

## 2. Configure and prepare local services

Reuse the existing Phase 1 .env and PostgreSQL volume. A new installation can
generate credentials with setup_env.py, then follow README.md for PostgreSQL,
Ollama, schema initialization and ingestion.

Both servers read .env from the repository directory, independently of the
client's working directory. Explicit process environment variables take precedence.

| Variable | Purpose |
| --- | --- |
| DATABASE_URL | Existing postgresql+psycopg connection URL |
| API_KEY | Existing Phase 1 secret, required by the shared Settings object |
| OLLAMA_URL | Local embeddings service, defaults to http://127.0.0.1:11434 |
| EMBEDDING_MODEL | Existing nomic-embed-text model, defaults to nomic-embed-text:latest |
| PAPER_PORTFOLIO_ID | Existing virtual account to trade, defaults to 1 |
| MAX_TRADE_QUOTE_AGE_SECONDS | Maximum executable quote age, defaults to 1200, allowed 1–86400 |

The stdio client launches each server as the current OS user and accesses its
database credentials through that process. API_KEY remains part of the Phase 1
configuration. Stdio tool calls use the process connection itself as the trust
boundary. The HTTP API continues to require X-API-Key.

The finance server needs local PostgreSQL and Ollama. The market server needs
internet access to Yahoo for quotes and local PostgreSQL for trade execution.
Only ticker requests go to the market provider. Financial search results are
returned to the MCP client, whose configured LLM may receive that context.

Start existing services as needed:

~~~powershell
docker compose up -d --wait postgres
ollama pull nomic-embed-text:latest
~~~

Keep Ollama running in another terminal if its desktop service is unavailable.

## 3. Upgrade the existing ledger

Run once before enabling paper execution. Re-running the migration is supported.

~~~powershell
.\.venv\Scripts\python.exe upgrade_phase2.py
~~~

The migration adds nullable request_id (UUID), nullable execution_metadata
(JSONB), and a unique request-ID index to virtual_orders. Existing Phase 1 rows
remain readable and may retain NULL values. A database initialized from the
updated models already has these columns. The migration uses transactional DDL,
an advisory lock and a ten-second lock timeout.

## 4. Run either server standalone

~~~powershell
.\.venv\Scripts\python.exe mcp_servers/finance_kb_mcp.py
~~~

Or, in another terminal:

~~~powershell
.\.venv\Scripts\python.exe mcp_servers/market_trading_mcp.py
~~~

The process waits for MCP JSON-RPC on stdin. Stdout carries protocol messages.
Server diagnostics go to stderr. Ctrl+C stops a manually launched process.
A successful launch is normally quiet.

For interactive testing, install Node.js and use the official MCP Inspector.
Run from the repository root:

~~~powershell
npx -y @modelcontextprotocol/inspector .\.venv\Scripts\python.exe mcp_servers/finance_kb_mcp.py
npx -y @modelcontextprotocol/inspector .\.venv\Scripts\python.exe mcp_servers/market_trading_mcp.py
~~~

Run one command at a time. Open the Inspector URL printed in the terminal,
connect using stdio, select Tools, and list/call the tools. The Inspector also
accepts the absolute Python executable and script paths from the desktop template.

## 5. Tool calls

### search_financial_kb

~~~json
{"query": "What grocery purchases did I make?", "top_k": 5}
~~~

The response contains a results list of chunk IDs, content, metadata and cosine
similarity. Queries allow 1–2000 characters and top_k allows 1–50. Ingested bank
transaction rows and financial notes share the existing vector index. Exact
financial totals require SQL aggregation. Retrieved content is untrusted source
material. Model digest compatibility checks remain active.

### get_stock_quote

~~~json
{"ticker": "AAPL"}
~~~

The quote includes Decimal price strings, currency, instrument type, UTC as_of
and fetched_at timestamps, age_seconds, and session OHLC/volume metrics. Price
comes from the latest available unadjusted regular-session one-minute bar,
whose timestamp is the start of that bar. Historical bars may be returned while
the exchange is closed. Session metrics cover the latest session in the fetched
five-day bar window. Yahoo data availability and delays vary by instrument.

The tool uses a 15-second timeout per history request. Provider retries and
metadata lookups can extend the overall call. Missing quotes, invalid symbols,
provider failures and rate limits become MCP tool errors.

### execute_paper_trade

~~~json
{
  "ticker": "AAPL",
  "shares": 1.25,
  "order_type": "BUY",
  "request_id": "ef8a223b-cbf5-4566-90f9-a768dcecc1e1"
}
~~~

For a sale use SELL with the number of shares to sell. Tickers and sides are
normalized to uppercase. Quantity must be finite, positive and have at most
eight decimal places. Stocks and ETFs execute in the portfolio's currency.
Currency mismatches require a separate FX policy. Freshness is checked again
after acquiring the portfolio lock, so market-closed quotes usually fail execution
under the default 20-minute age limit.

Execution uses the latest available bar price, rounded to eight decimals.
Settlement rounds quantity times fill price to cents using ROUND_HALF_UP.
Settled trade value must be at least 0.01. BUY requires sufficient virtual cash.
SELL requires sufficient held shares. Weighted average buy price updates on buys.
A full sale closes the position, and a later buy reopens the existing ticker row.
An executed VirtualOrder has status CLOSED, and its buy_price stores the fill
price for either side. No commissions or slippage are applied.

The receipt includes order_id, request_id, fill_price, settled_notional, updated
cash, resulting position shares, timestamps and replayed. Decimal values are
strings. Receipts are returned after a successful database commit. Cash, holdings
and the order/receipt are written atomically under a portfolio FOR UPDATE lock.

Generate a UUID before sending a trade and reuse it with identical arguments
after a timeout or connection failure. A committed retry returns the saved
receipt with replayed=true, including its original post-trade balances.
Reusing a key for different arguments fails. If request_id is omitted, the server
generates one and each call creates a new order. Keep the returned ID for retries.
A retry whose original result was lost requires the caller-supplied key.

Concurrent trades against one portfolio serialize under the row lock. Lock and
statement timeouts fail the entire transaction. All ledger writers must follow
the same locking rule. Cancellation waits for worker completion, though a client
can disconnect after a commit. Use request IDs to resolve uncertain outcomes.

The existing GET /api/v1/trading/portfolio endpoint reflects successful fills.
Its default portfolio ID is 1. Pass ?portfolio_id=... if PAPER_PORTFOLIO_ID differs.

## 6. Configure Claude Desktop or another local MCP client

Copy entries from claude_desktop_config_template.json into the client's MCP
configuration. Replace C:/ABSOLUTE/PATH/finbrain everywhere with the real absolute
repository path. Windows paths with forward slashes are valid JSON. On macOS
or Linux use /absolute/path/finbrain/.venv/bin/python as command and the matching
absolute script path as the single args entry.

Merge the mcpServers entries into an existing configuration, preserving other
servers. In Claude Desktop use Settings → Developer → Edit Config where available,
save, and restart the client. It launches each server automatically. Tool names
should appear after both stdio handshakes complete.

The template keeps secrets in the repository .env. It does not require a working
directory setting. Environment overrides can be added to an entry's env object.
Use the private database credentials created during Phase 1 and keep the .env
file and local financial records in private storage.

## Docker launch option

The Windows host in this workspace blocked a pandas binary under application
control. The complete suite runs successfully in the Python 3.11 Linux image.
The existing Compose API service can also host either stdio MCP server.

After preparing .env and starting PostgreSQL:

~~~powershell
docker compose --profile app build api
docker compose --profile app run --rm --no-deps -T api python upgrade_phase2.py
docker compose --profile app run --rm --no-deps -T api python mcp_servers/finance_kb_mcp.py
~~~

For the trading server replace the script with mcp_servers/market_trading_mcp.py.
In a desktop client's server entry, use command "docker" and these args for
the finance server, substituting the absolute project directory:

~~~json
[
  "compose",
  "--project-directory", "C:/ABSOLUTE/PATH/finbrain",
  "--profile", "app",
  "run", "--rm", "--no-deps", "-T",
  "api", "python", "mcp_servers/finance_kb_mcp.py"
]
~~~

Compose supplies the database URL and host Ollama address from the existing
service configuration. The -T flag preserves protocol pipes without allocating
a terminal. Keep Docker running while the client uses these servers.
## 7. Verify behavior

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
~~~

Validation completed with Python 3.11, MCP 2.3.0 and yfinance 1.8.0 in Docker.
All 42 tests passed, including real PostgreSQL tests. A live AAPL quote also
succeeded through the stdio client/server connection.

The suite covers both real stdio handshakes and tool discovery from a different
working directory, typed argument validation, structured output, safe error
messages, Yahoo bar normalization, currency/freshness checks and average cost.

For database execution tests, point TEST_DATABASE_URL at an initialized disposable
PostgreSQL/pgvector database after the Phase 2 migration:

~~~powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://finance_app:YOUR_TEST_PASSWORD@127.0.0.1:5432/finance_auditor_test"
.\.venv\Scripts\python.exe -m pytest tests/test_phase2_postgres.py tests/test_postgres.py -q
~~~

Tests create isolated virtual accounts and remove their own records afterward.
They check concurrent overspending/overselling prevention, duplicate-request
serialization, rollback after an injected SQL failure, retry receipts and
compatibility with legacy orders. Quote prices are deterministic mocks in these
tests, so execution tests work independently of market hours and Yahoo availability.

References follow the APIs used here.
[Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk),
[MCP stdio transport](https://modelcontextprotocol.io/specification/latest/basic/transports),
[yfinance documentation](https://ranaroussi.github.io/yfinance/).
