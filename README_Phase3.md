# Phase 3 — Orchestrating Agent and Paper Trading Loop

Phase 4 supersedes provider selection with LLM_CLOUD_* settings and per-request
routing modes. Follow [Phase 4 configuration](README_Phase4.md) for cloud setup.

The authenticated POST /api/v1/agent/chat endpoint runs a custom asynchronous
model/tool/observation/synthesis loop. It combines financial retrieval, market
quotes, virtual cash/holdings checks, and paper execution.

The registry discovers the Phase 2 MCP schemas and dispatches those tools in
process. It adds get_portfolio and a constrained execution wrapper. PostgreSQL
stores conversation history, tool arguments, observations, timestamps and
resumable checkpoints. Existing stdio servers remain available to external clients.

## 1. Install and migrate

Reuse the Phase 1/2 database and .env. From the repository root:

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
docker compose up -d --wait postgres
.\.venv\Scripts\python.exe upgrade_phase2.py
.\.venv\Scripts\python.exe upgrade_phase3.py
~~~

On macOS/Linux use .venv/bin/python. For a new installation, follow README.md
first to initialize PostgreSQL, generate secrets and ingest the fictional data.

The Phase 3 migration creates agent_conversations and agent_turns. It supports
reruns and preserves the existing ledger. The application role must own its
tables and have the schema privileges established in Phase 1.

## 2. Select a model

The adapter uses the official OpenAI Python SDK. Local mode uses a schema-constrained
ReAct decision through Chat Completions. Remote mode uses native Responses API
function calling. Both return the same internal tool-call representation.
The default endpoint is local Ollama. Configure a locally installed model
with structured outputs and an Ollama version supporting JSON-schema response_format
on /v1/chat/completions.

~~~powershell
ollama list
ollama pull llama3.1:8b
ollama create finbrain-agent -f agent/Modelfile
~~~

The pull command is needed if that base model is absent. The alias reuses its
weights and configures a 16K context window for multi-step observations.
This is the configuration used in the successful local paper-trade smoke test.
Keep Ollama running.
Add or update these values in .env:

~~~dotenv
AGENT_LLM_BASE_URL=http://127.0.0.1:11434/v1
AGENT_MODEL=finbrain-agent
AGENT_LLM_API_KEY=local-only
AGENT_ALLOW_REMOTE_LLM=false
AGENT_LOCAL_JSON_MODE=true
AGENT_MODEL_TIMEOUT_SECONDS=120
~~~

AGENT_MODEL is intentionally empty in .env.example until you select a model.
The embedding model remains nomic-embed-text. Restart the API after changing
settings. The local decision schema limits each round to a registered tool with validated
arguments or a final answer. The application validates the returned JSON again
before checkpointing and dispatch. Plain prose is never interpreted as an action.
Set AGENT_LOCAL_JSON_MODE=false for a local server with reliable native Responses
function calling. Without tool observations, the loop makes a bounded correction
attempt and then returns a failed turn.

For OpenAI-hosted inference, explicitly configure HTTPS, your API key, an
available tool-capable model and remote opt-in:

~~~dotenv
AGENT_LLM_BASE_URL=https://api.openai.com/v1
AGENT_LLM_API_KEY=YOUR_OPENAI_API_KEY
AGENT_MODEL=YOUR_TOOL_CAPABLE_OPENAI_MODEL
AGENT_ALLOW_REMOTE_LLM=true
~~~

Remote mode sends conversation context and tool observations to that provider.
Local mode requires your model server to execute the selected model locally.
The adapter disables HTTP proxy inheritance and redirects. It sets store=false,
replays protocol items itself and preserves opaque reasoning items in native Responses mode.
[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling),
[OpenAI conversation state](https://developers.openai.com/api/docs/guides/conversation-state),
[Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility),
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs).

## 3. Run the API

~~~powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
~~~

The router in main_agent.py is mounted by main.py and inherits X-API-Key
authentication. Open http://127.0.0.1:8000/docs and authorize with API_KEY.

For Docker, rebuild the existing API service after the dependency changes:

~~~powershell
docker compose --profile app build api
docker compose --profile app run --rm api python upgrade_phase3.py
docker compose --profile app up -d api
~~~

When the model runs on the Docker host, set AGENT_LLM_BASE_URL in .env to
http://host.docker.internal:11434/v1 before starting the Docker API. The existing
Compose service already configures its database and local embedding addresses.
The Python 3.11 Linux image also supports hosts with the Windows pandas binary
restriction observed during Phase 2.

## 4. Send a query

A minimal request contains message and conversation_id. Supply a UUID request_id
when sending any turn that may trade so a lost HTTP response can be retried safely.

~~~json
{
  "message": "Check Apple stock price, verify my virtual cash balance, and buy 10 shares of AAPL if I have enough paper money.",
  "conversation_id": "paper-demo",
  "request_id": "d68be46a-033c-4163-a82e-d1b124f588ba"
}
~~~

Save this as chat_request.json. In Bash:

~~~bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/agent/chat   -H "X-API-Key: $API_KEY"   -H 'Content-Type: application/json'   --data-binary @chat_request.json
~~~

In PowerShell:

~~~powershell
$apiKeyLine = Get-Content .env | Where-Object { $_ -match '^API_KEY=' }
$headers = @{ "X-API-Key" = $apiKeyLine.Substring(8) }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/agent/chat -Headers $headers -ContentType application/json -InFile chat_request.json
~~~

For an expense/trading query, try a new request UUID with the same conversation ID:

~~~json
{
  "message": "Review my grocery purchases. Check my virtual cash and AAPL quote, then buy 2 paper shares only if the fill price is at most USD 200 and the total cost is at most USD 400. Explain what happened.",
  "conversation_id": "paper-demo",
  "request_id": "1e8c72b2-5436-4b0a-aa7d-46d1c3683303"
}
~~~

For a sale, explicitly request SELL and the quantity. For analysis, ask for
retrieval or quotes. The agent instructions restrict execution to the current
user's execution request. Model interpretation determines which tool arguments
represent the natural-language instruction. Review those arguments in the trace.

The loop reads actual portfolio and quote observations. The ledger independently
checks cash, held shares, quote freshness, currency and optional maximum buy price,
minimum sell price and maximum settled notional inside its locked transaction.
A model's answer alone cannot establish that a fill occurred.

## 5. Response and trace

The JSON response contains these fields.

| Field | Meaning |
| --- | --- |
| answer | Model analysis or receipt-based trade summary, with any failure/limit explanation |
| status | completed, failed or limit_reached |
| conversation_id / request_id | Persistent conversation and turn identifiers |
| steps | Ordered tool calls with arguments, observations, status and UTC timestamps |
| started_at / completed_at | UTC timestamps for this turn |
| portfolio | Updated cash, holdings, indicative valuations and P&L |
| portfolio_error | An explanation when the final portfolio snapshot failed |
| replayed | True when returning a previously completed turn |

Each successful execution observation contains the Phase 2 order receipt, including
order_id, execution request_id, fill_price, settled_notional and resulting cash.
Tool rejections are explicit error observations that the model can use to
explain or revise its plan. The trace records observable actions and results. For trade turns, the application
renders the answer from retrieved evidence, quotes, actual receipts and error
observations. This keeps model-generated execution claims out of the final trade
summary. Read-only turns use the model's synthesis.

Conversation history and the full trace are private JSONB data in PostgreSQL.
The prompt receives a bounded recent history of up to 20 user/assistant pairs.
Older turns and full execution checkpoints remain stored. Model-facing tool
observations above 20,000 characters are shortened, with the full observation
retained in the returned trace. Local mode projects completed observations into
a compact context with up to five financial matches and 600 characters per match.
Application logs omit financial contents.

## 6. Retry and concurrency behavior

Reuse exactly the same conversation_id, request_id and message after a timeout,
disconnect or checkpoint storage error. A completed request returns the saved
response. Reusing an ID for different chat arguments returns 409.

Each tool plan is checkpointed before dispatch. Paper execution derives a stable
UUID from the chat request and normalized trade arguments. Identical trades in
one turn replay a single fill, even if the model emits a second call ID.
For an additional intended identical trade, send a new chat request UUID.

A crash after the ledger commits but before the observation is saved leaves a
durable pending action. Retrying the turn dispatches that action with the same
execution key and recovers its receipt. Confirmed fills remain committed if a
later model request fails. The returned failed response includes those tool steps.

A database advisory lock serializes each conversation across processes.
Concurrent requests for that conversation return 409. After an interrupted turn,
resume its request ID before starting a new turn. Four active agent turns are
allowed per process. Checkpoints use short transactions, and a separate lock
connection remains held for the turn.

Default bounds are 8 model rounds, 16 tool calls, 2,000 output tokens per model
response, and 100,000 input-context characters. Configure AGENT_MAX_STEPS,
AGENT_MAX_TOOL_CALLS, AGENT_MAX_OUTPUT_TOKENS and AGENT_MAX_CONTEXT_CHARS if needed.
AGENT_MODEL_TIMEOUT_SECONDS applies to each model request. A turn may take
several model/tool round trips. Empty or incomplete model responses return a
failed turn with the available trace.

## 7. Portfolio P&L

Accounting uses Decimal and the Phase 2 average-cost convention. Realized P&L
replays completed orders in order-ID sequence, using saved settlement notionals
where available. Current position quantities and average costs must agree with the reconstructed
ledger. Incomplete or inconsistent histories produce a null realized P&L.

For open positions, market value uses the latest available Yahoo bar. Unrealized
P&L is market value less rounded average-cost basis. Equity is virtual cash plus
market value. Total P&L combines realized and unrealized P&L when both are available.

Each holding reports quote_as_of, quote_stale and any quote_error. Delayed or
market-closed quotes provide indicative valuations with their timestamps.
Unavailable or currency-mismatched quotes produce null aggregate equity,
unrealized P&L and total P&L while cash and cost basis remain available.
Concurrent activity may change the portfolio after its snapshot timestamp.

## 8. Tests

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
~~~

Set TEST_DATABASE_URL to an initialized disposable PostgreSQL database and rerun
to include persistence, concurrency, P&L and crash-recovery tests. Tests create
isolated virtual accounts and conversations and remove their own records.
SDK requests for both decision modes use a mock HTTP transport, so automated tests need no paid model
credentials or market-hours availability.

Validation completed with Python 3.11 in Docker and a real PostgreSQL/pgvector
database. All 67 tests passed. A local Llama 3.1 model with a 16K context window
completed quote → cash check → one simulated AAPL buy, and repeating the same
chat request returned the saved response. Earlier small-model trials reached
the configured step limit on richer prompts. Planning quality depends on the
selected model. Native cloud Responses calls are covered by mocked SDK tests.
