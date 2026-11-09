# Private Finance Auditor — Phase 1

For the MCP servers and paper execution, see [Phase 2 setup](README_Phase2_MCP.md).
For the conversation agent and chat endpoint, see [Phase 3 setup](README_Phase3.md).

A single-owner financial retrieval API and virtual portfolio schema.
Financial text is embedded by local Ollama (nomic-embed-text, 768 dimensions).
PostgreSQL stores text, JSONB metadata, and vectors with a cosine HNSW index.
Paper portfolios start with **USD 100,000.00**, and position/order tables support
fractional shares. Money uses PostgreSQL NUMERIC and Python Decimal.

This phase provides retrieval and a read-only portfolio ledger. Order execution,
fills, cash settlement, brokerage integrations, market prices, and generated RAG
answers are future phases. No order placement API exists. Holdings include cost
basis, not current market value or trading performance.

## 1. Prerequisites and secrets

Install Python 3.11+, Docker with a running Linux-container engine, and Ollama.
On Windows, start Docker Desktop before the following commands.

This repository may be in a OneDrive directory. For private financial records,
move the project and its .env outside cloud-synced folders before using real
data. Git ignores do not prevent OneDrive synchronization.

Create a virtual environment (PowerShell):

~~~powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_env.py
~~~

On macOS/Linux use python3 -m venv .venv and .venv/bin/python in place of
.\.venv\Scripts\python.exe.

setup_env.py generates independent random admin, application, and API secrets.
It refuses to overwrite an existing .env. Generated hexadecimal database
passwords are safe in connection URLs. If setting your own passwords, URL-encode
reserved characters in DATABASE_URL; the Compose API URL expects URL-safe
APP_DB_PASSWORD. .env.example documents settings; never commit .env.

## 2. Start PostgreSQL

~~~powershell
docker compose up -d --wait postgres
~~~

The first volume initialization enables vector and creates finance_app
with schema privileges but no superuser/role/database-creation powers.
The admin credentials are used only for initialization/administration. Data
persists in the postgres_data named volume. Port 5432 is bound to loopback.

If a volume already exists, Docker's initialization scripts do not rerun.
Changing .env passwords does not rotate existing database roles. Rotate roles
with an administrative connection and update .env together; do not delete a
volume containing real data to resolve a password mismatch.

## 3. Prepare the local embedding model

~~~powershell
ollama pull nomic-embed-text:latest
ollama list
~~~

Ollama must be running at http://127.0.0.1:11434. If necessary start it with
ollama serve in a separate terminal. Model/image/package downloads require
internet access; financial text is processed locally after setup. The application
does not call a cloud embedding or LLM provider, follow embedding HTTP redirects,
or use environment-configured HTTP proxies.

The model name/tag must match EMBEDDING_MODEL. Database initialization records
the local model digest, 768 dimensions, and retrieval-prefix convention.
Ingestion and search reject a different model digest. Keep model artifacts stable
while the service runs. For a model upgrade, use a fresh database, initialize it
with the new model, and reingest original sources before switching databases.
create_all is initial bootstrap, not a schema migration system; future schema
changes should use versioned migrations.

## 4. Initialize schema and ingest mock data

~~~powershell
.\.venv\Scripts\python.exe database.py
.\.venv\Scripts\python.exe ingest_finance.py --csv data/sample_transactions.csv --notes data/sample_notes.txt
~~~

The sample includes 10 fictional USD transactions and one short financial note.
Re-running the same command skips already imported chunks.

CSV contract:

- UTF-8 (optional BOM); columns Date,Description,Amount,Category.
- ISO dates (YYYY-MM-DD), plain signed decimal amounts, at most two decimal places.
- Negative amounts represent outflows; positive amounts represent income/refunds.
- Quote descriptions containing commas. Thousands separators, currency symbols,
  and bank-specific date formats must be normalized before import.
- Amount metadata is a decimal string; no money is converted through float.
- Use --source-type credit_card for credit-card exports.
- Repeat --csv and --notes for multiple files. Notes are UTF-8 text.

CSV rows remain atomic transaction chunks; notes use overlapping character
chunks. Ollama rejects oversized inputs instead of silently truncating them.
Import validation/embedding/SQL failures roll back the entire import; only
record counts are printed.

Deduplication keys include absolute input path, row/chunk offset, content, and
metadata. This preserves identical legitimate transactions at different rows.
Moving a file or editing/reordering it can produce new records; importing a
revised statement is append-only and does not reconcile or delete old rows.

Keep real files in data/private/ (ignored by Git and the Docker build). The CLI
loads parsed input in memory and writes in embedding batches of 32; very large
exports should be split into manageable files.

## 5. Run the API

~~~powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
~~~

Open http://127.0.0.1:8000/docs, click **Authorize**, and enter API_KEY from .env.
Both data endpoints require X-API-Key; the Swagger shell/schema are public.
Settings are read at process startup; restart after changing secrets.

PowerShell examples (run in a separate terminal):

~~~powershell
$apiKeyLine = Get-Content .env | Where-Object { $_ -match '^API_KEY=' }
$headers = @{ "X-API-Key" = $apiKeyLine.Substring(8) }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/finance/query -Headers $headers -ContentType application/json -Body '{"query":"What grocery purchases did I make?","top_k":5}'
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/trading/portfolio -Headers $headers
~~~

Equivalent curl commands in Bash (set API_KEY to your generated value first):

~~~bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/finance/query \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"What grocery purchases did I make?","top_k":5}'

curl --fail-with-body http://127.0.0.1:8000/api/v1/trading/portfolio \
  -H "X-API-Key: $API_KEY"
~~~

Search returns {"results":[{"id":"...","content":"...","metadata":{},"similarity":0.8}]}.
Similarity is 1 - cosine_distance in [-1, 1], not a probability. HNSW provides
approximate nearest neighbors. top_k is 1–50; queries are 1–2000 characters.
An empty index returns an empty list. Semantic search cannot reliably compute
totals or exhaustive audits; use SQL aggregates for financial calculations.

Portfolio response starts with:

~~~json
{
  "id": 1,
  "currency": "USD",
  "cash_balance": "100000.00",
  "holdings_cost_basis": "0",
  "updated_at": "2026-11-09T00:00:00Z",
  "holdings": []
}
~~~

The timestamp above is illustrative. Decimal fields serialize as strings to
preserve precision. ?portfolio_id=2 selects a different existing portfolio;
missing portfolios return 404. No fake holdings are seeded.

## Optional: API and CLI in Docker

After generating .env, starting PostgreSQL, and pulling the host Ollama model:

~~~powershell
docker compose --profile app build api
docker compose --profile app run --rm api python database.py
docker compose --profile app run --rm api python ingest_finance.py --csv data/sample_transactions.csv --notes data/sample_notes.txt
docker compose --profile app up -d api
~~~

The API uses postgres:5432 and host.docker.internal:11434 inside Docker,
and remains exposed only on host loopback port 8000. Docker Desktop normally
provides host-service routing. On native Linux, the host gateway may not reach a
loopback-only Ollama listener. Prefer the host Python workflow in that case;
do not expose Ollama publicly. Private input files are intentionally excluded
from images; use the host CLI or a narrowly scoped read-only bind mount.

## Tests and verification

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
~~~

Tests cover authentication, request limits, Decimal parsing, duplicate-row
identity, note boundaries, vector validation, and PostgreSQL schema compilation.
They use mocked local embeddings and database sessions without downloading models.

For the optional real database smoke test, initialize a disposable test database
as above, set TEST_DATABASE_URL to its SQLAlchemy connection URL, and rerun pytest.
That test checks actual vector cosine search, conflict handling, and cash
constraints inside a transaction that is rolled back.

Initial workspace validation: 18 tests passed; the real database test was skipped
because Docker's engine was unavailable. Live PostgreSQL/Ollama ingestion and the
Docker image still require a running local environment.

## Security and operational boundaries

This is a single-owner local foundation. Every data request uses one API key;
there is no multi-user isolation. The application database role owns Phase 1
tables, so use separate migration and restricted runtime roles before a broader
deployment. Future execution code must lock the portfolio row and atomically
update cash, holdings, and order status; the schema alone does not enforce that
cross-table accounting invariant. Order buy_price is the simulated execution
price for both BUY and SELL; CLOSED means completed in this initial schema.

Raw documents, metadata, and embeddings are stored in PostgreSQL. They are not
application-encrypted, and embeddings are sensitive. Use full-disk encryption,
encrypted backups, private filesystem permissions, and tested backup restores.
Loopback HTTP/PostgreSQL connections are local; remote deployment requires TLS,
network access controls, and proper authentication/rate limiting. Do not place
this service or Ollama directly on the public internet.

Stop containers with docker compose --profile app down; this retains the
database volume. Back up data before any destructive volume operations.

References: [pgvector SQLAlchemy integration](https://github.com/pgvector/pgvector-python#sqlalchemy),
[Ollama embedding API](https://docs.ollama.com/api/embed).
