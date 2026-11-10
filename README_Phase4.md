# Phase 4 — Hybrid LLM Infrastructure and Routing

The agent now binds a provider and tool policy to each persisted turn.
A deterministic classifier evaluates privacy signals, public ticker context,
complexity and the selected routing mode. It runs locally without an LLM request.
Cloud access defaults to disabled.

## 1. Install and prepare Ollama

Reuse the PostgreSQL database, API_KEY and Phase 3 setup.

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull nomic-embed-text
ollama pull qwen2.5-coder:7b
~~~

A lighter locally installed model such as llama3.2 can also be selected.
The existing Phase 3 finbrain-agent alias remains usable. For multi-step tasks,
configure a sufficiently large model context window. The Phase 3 alias uses 16K.

Set Ollama's own environment before starting its service:

~~~powershell
$env:OLLAMA_NO_CLOUD = "1"
$env:OLLAMA_CONTEXT_LENGTH = "16384"
ollama serve
~~~

For an existing desktop service, apply those variables to its user environment
and restart Ollama. On Linux, configure them in its service environment. Model
downloads require internet access during setup. Downloaded weights support local
inference afterward. The application rejects cloud-tagged models and model
metadata identifying a remote backend before generation or embedding.
[Ollama local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features).

## 2. Configure privacy and local inference

Add/update these settings in .env:

~~~dotenv
OLLAMA_URL=http://127.0.0.1:11434
EMBEDDING_MODEL=nomic-embed-text:latest
LLM_ROUTING_MODE=auto
LLM_LOCAL_MODEL=qwen2.5-coder:7b
LLM_LOCAL_FALLBACK_MODEL=
LLM_CLOUD_ENABLED=false
LLM_OFFLINE_MODE=false
AGENT_LOCAL_JSON_MODE=true
~~~

An empty LLM_LOCAL_MODEL inherits AGENT_MODEL from Phase 3. The optional
LLM_LOCAL_FALLBACK_MODEL selects another downloaded local model if the primary
fails. Local calls always use OLLAMA_URL. Phase 3 remote AGENT_LLM_BASE_URL and
AGENT_ALLOW_REMOTE_LLM settings cannot redirect private inference.

Local generation uses schema-constrained ReAct decisions by default. Native
Responses mode remains selectable with AGENT_LOCAL_JSON_MODE=false.
The asynchronous OllamaClient also exposes generate(prompt) and embed(texts).
Financial search and ingestion keep their existing nomic retrieval prefixes,
768-dimensional schema and embedding digest checks.

Set LLM_OFFLINE_MODE=true for fully local processing after setup. This disables
cloud inference, Yahoo requests and new paper execution. Existing paper receipts
remain recoverable by request ID. Local ledger snapshots and financial retrieval
remain available. Market valuation fields become null when quotes are unavailable.

With offline mode false, requested market workflows may contact Yahoo for
approved ticker quotes. Local-only describes the LLM route. Expense-only audits
perform their portfolio snapshot without fetching external market valuations.

## 3. Optionally enable cloud market reasoning

Choose one provider and an explicitly available model.

~~~dotenv
LLM_CLOUD_ENABLED=true
LLM_CLOUD_PROVIDER=openai
LLM_CLOUD_MODEL=YOUR_AVAILABLE_OPENAI_MODEL
LLM_CLOUD_API_KEY=YOUR_PROVIDER_API_KEY
~~~

For Anthropic use LLM_CLOUD_PROVIDER=anthropic and the corresponding model/key.
Both SDKs are included in requirements.txt. Claude 3.5 Sonnet was retired on
Anthropic's API on October 28, 2025. Select a model available in your account.
The application preserves the configured ID and handles provider failures through
the selected fallback policy.
[Anthropic model lifecycle](https://platform.claude.com/docs/en/about-claude/model-deprecations).

Cloud requests receive a generated task, approved public ticker symbols,
an analysis type/horizon, approved public drivers and validated quote fields.
Raw user wording, conversation history, financial documents, bank metadata,
portfolio quantities, cash balances and paper receipts remain on this server.

This public projection deliberately limits free-form market instructions.
Use public_market_context to specify the intended analysis explicitly. Actual
personal/virtual portfolio reasoning stays local. A hypothetical unweighted basket
of public tickers can use the cloud portfolio_analysis task.

OpenAI uses the Responses API with store=false. Anthropic uses Messages tool_use
blocks. Each cloud round reconstructs a fresh public-only context from validated
quote observations. Cloud capabilities are limited to get_stock_quote.
Analysis uses timestamped quote bars and general model knowledge.
[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling),
[Anthropic tool calling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).

## 4. Routing modes and controls

| Request mode | Behavior |
| --- | --- |
| auto | Private/ambiguous/routine queries use local inference. Complex approved public market tasks can use cloud when configured. |
| local_only | All model work uses local Ollama. |
| cloud_only | Approved public market tasks use the configured cloud provider. Private, ambiguous, offline or disabled-cloud requests return 403. |

Additional server controls are available:

| Variable | Default | Purpose |
| --- | --- | --- |
| LLM_COMPLEXITY_THRESHOLD | 2 | Complexity threshold for automatic cloud selection |
| LLM_CLOUD_FALLBACK_LOCAL | true | Auto-mode cloud failures can fall back locally |
| LLM_PUBLIC_CLOUD_FALLBACK | false | Explicit opt-in for auto-mode public local failures to use cloud |
| LLM_PUBLIC_TICKERS | Built-in public symbols | JSON array overriding approved agent market symbols |

Complexity considers analysis terms, multiple symbols and query length.
Risk/scenario/portfolio analysis context is treated as complex. Classification
is conservative and can route an unclear public query locally. Privacy protection
also comes from the generated cloud projection and restricted tool dispatcher.

The approved symbol list covers common US stocks and ETFs. To extend it, include
all desired symbols in the override:

~~~dotenv
LLM_PUBLIC_TICKERS=["AAPL","MSFT","TSLA","NVDA","AMD","SPY","QQQ"]
~~~

Both model-requested quotes and automatic agent valuation respect this list.
Direct Phase 2 MCP consumers retain their existing interfaces and share the
offline guard. Their external model-client context policies are controlled by
those clients.

A conversation containing private or legacy unclassified history remains local
for later turns. Use a new conversation ID for an isolated public analysis.
The server stores privacy provenance in existing JSONB checkpoints, so Phase 3
data remains compatible. Routing options join the request-ID idempotency contract.

## 5. Run and inspect routing

~~~powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
~~~

For the existing Docker setup:

~~~powershell
docker compose up -d --wait postgres
docker compose --profile app build api
docker compose --profile app up -d api
~~~

Compose already sets OLLAMA_URL to the host Ollama service inside the API
container. The new wrapper derives its generation address from that value.
Existing Phase 3 database setup continues to apply.

All endpoints inherit X-API-Key authentication. Swagger at
http://127.0.0.1:8000/docs exposes routing_mode as a selectable field.

~~~bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/llm/status   -H "X-API-Key: $API_KEY"

curl --fail-with-body http://127.0.0.1:8000/api/v1/llm/route   -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json'   -d '{"message":"Compare AAPL and MSFT market risk","routing_mode":"auto"}'
~~~

The preview evaluates the current query without provider calls or database reads.
The actual chat decision additionally considers persisted conversation provenance
and pending tool context.

## 6. Send agent requests

Private auditing always stays local:

~~~json
{
  "message": "Review my bank statement for recurring expenses.",
  "conversation_id": "private-audit",
  "request_id": "68990ec1-3771-46d5-9741-590d91ec831f",
  "routing_mode": "auto"
}
~~~

A public market request can use cloud when enabled:

~~~json
{
  "message": "Compare AAPL and MSFT under rising interest rates.",
  "conversation_id": "public-market-analysis",
  "request_id": "3207a7f6-5099-4e73-90ec-c3e5712f8e0",
  "routing_mode": "auto",
  "public_market_context": {
    "tickers": ["AAPL", "MSFT"],
    "analysis": "scenario",
    "horizon": "long_term",
    "drivers": ["interest_rates", "valuation"]
  }
}
~~~

Save either object to chat_request.json and submit it:

~~~bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/agent/chat   -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json'   --data-binary @chat_request.json
~~~

For PowerShell:

~~~powershell
$apiKeyLine = Get-Content .env | Where-Object { $_ -match '^API_KEY=' }
$headers = @{ "X-API-Key" = $apiKeyLine.Substring(8) }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/agent/chat -Headers $headers -ContentType application/json -InFile chat_request.json
~~~

Analysis types are overview, comparison, risk, scenario and portfolio_analysis.
Horizons are short_term and long_term. Drivers are interest_rates, inflation,
earnings, valuation, diversification, recession, ai_demand, trade_policy,
regulation and liquidity. These enums preserve useful public reasoning context
without forwarding arbitrary user text.

Responses retain the Phase 3 answer, execution trace and portfolio fields.
They add routing metadata and routing_events recording backend/model, reason,
success/failure, UTC start time and measured duration_ms. Events contain no
prompt or observation text. Existing full private tool traces remain in PostgreSQL.

Reuse the same message, conversation ID, request ID and routing options on retry.
Changing routing options with the same request ID returns 409. A completed
request returns its saved routing and result. Private mid-turn tool context
blocks further cloud dispatch and auto mode moves to local inference.

## 7. Benchmark latency and cloud usage

Measure classifier overhead without contacting models:

~~~powershell
.\.venv\Scripts\python.exe -m llm.benchmark --iterations 1000
~~~

To compare actual agent latency, start the API and explicitly run live requests:

~~~powershell
.\.venv\Scripts\python.exe -m llm.benchmark --live --iterations 3 --modes auto local_only cloud_only
~~~

The live command can incur provider charges when cloud is enabled. It uses fixed
read-only examples and fresh request IDs. The report contains successful-sample
p50/p95 HTTP latency, per-request model duration, backend call counts, blocked
requests and failures. Prompt/answer contents are omitted.

Warm each model before collecting steady-state samples. Compare equivalent public
tasks under local_only and cloud_only using fresh conversation IDs. Exclude
failures and blocked samples. Review both latency and answer quality. Local
hardware, model size, loading, network delays and tool round trips affect results.
Cloud call counts show routing usage. Actual billing follows your provider account.
The benchmark reports measurements rather than assuming a local latency advantage.

## 8. Verification

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
~~~

Set TEST_DATABASE_URL to an initialized disposable PostgreSQL/pgvector database
to include persistence and recovery tests. Cloud SDK tests use mock HTTP transports.
Tests cover payload isolation even when a heuristic misses a privacy marker,
cloud-only restrictions, legacy conversation privacy, mid-turn private context,
fallback directions, offline receipt recovery, and cloud-backed embedding rejection.

### Validation results

The complete suite passed with 97 tests on Python 3.11 in Docker against a
real disposable PostgreSQL 16/pgvector database. `pip check` reported no broken
requirements. Provider wire-format and private-payload isolation tests use mocked
OpenAI and Anthropic HTTP responses. Live paid cloud calls and comparative cloud
latency measurements were outside this validation run.

A live Ollama generation check succeeded. A separate fictional private bank audit
made zero cloud inference calls and zero external portfolio-valuation calls.
The installed small local model repeated a search until the agent step limit,
so that run did not complete a synthesized audit. Model choice and instruction
following affect end-to-end task completion. The agent returns its execution
trace and bounded-step status for inspection.

Cloud tool definitions are rebuilt from a fixed public quote facade. Local MCP
descriptions and arbitrary schema text are excluded from cloud requests.
