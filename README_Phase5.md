# Phase 5 evaluation framework

## Scope and privacy

The suite contains 18 fictional, fixed-date financial Q&A and hypothetical trading
cases. It sends questions to the normal `/api/v1/agent/chat` entry point, collects
actual retrieval/tool traces, and computes exact retrieval metrics. An optional
OpenAI or Anthropic judge scores groundedness and semantic correctness.

Use a dedicated evaluation database and API process. The harness requires the
API's authenticated `/api/v1/llm/status` to report `offline_mode=true` before any
chat requests. The shared trading service then blocks new paper fills and external
quotes. The trading cases reason about a fixed snapshot in the knowledge base.
Existing ledger/concurrency tests exercise actual execution and recovery.

The default `--judge none` makes no judge requests and leaves LLM scores null.
Cloud judging requires both `--allow-cloud-judge` and `EVAL_JUDGE_API_KEY` in the
process environment. This separate evaluation opt-in allows the question,
candidate answer, retrieved text, full tool trace, reference context and expected
answer to reach the selected judge provider. Phase 4's public-market projection
cannot grade private evidence, so it is not used for judging. The agent remains
local under the evaluation server's offline policy.

A dataset's `synthetic=true` declaration cannot prove that captured tool responses
contain only fictional data. Keep the evaluation database separate from personal
financial records. Reports include questions, answers, traces and metadata and
inherit the destination directory's filesystem permissions. Default reports under
`eval/reports/` are Git-ignored. Custom output paths require equivalent handling.
API keys are excluded from reports and console output.

## 1. Install dependencies

Run from the repository root with Python 3.11 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Phase 5 reuses the existing `httpx`, Pydantic, OpenAI and Anthropic dependencies.
It adds no evaluation framework package. The pipeline uses local Ollama generation
and `nomic-embed-text` embeddings as configured in Phase 4.

```powershell
ollama pull nomic-embed-text
ollama pull llama3.2
```

Use a local model with adequate context length and reliable tool calling.
Follow Phase 4's Ollama cloud-disable instructions for the model server itself.

## 2. Start an isolated database and seed the fixtures

These PowerShell commands create a dedicated PostgreSQL container with no
persistent volume. Choose a different container name or port if these are in use.
Removing this container later removes its evaluation data.

```powershell
docker run --detach --name finbrain-eval-db --env POSTGRES_DB=finance_eval --env POSTGRES_USER=finance_eval --env POSTGRES_PASSWORD=fictional-eval-only --publish 127.0.0.1:55432:5432 pgvector/pgvector:pg16
docker exec finbrain-eval-db pg_isready -U finance_eval -d finance_eval
```

Wait for `pg_isready` to report accepting connections. Set these variables in the
same terminal before initialization and keep them set for the API process.
The sample database password is for this disposable local fixture instance.

```powershell
$env:DATABASE_URL = 'postgresql+psycopg://finance_eval:fictional-eval-only@127.0.0.1:55432/finance_eval'
$env:API_KEY = 'fictional-eval-api-key-change-me-1234567890'
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:EMBEDDING_MODEL = 'nomic-embed-text:latest'
$env:LLM_LOCAL_MODEL = 'llama3.2:latest'
$env:LLM_OFFLINE_MODE = 'true'
$env:LLM_CLOUD_ENABLED = 'false'
python database.py
python upgrade_phase3.py
python -m eval.eval_harness --seed-fixtures --confirm-disposable-database
```

Initialization binds the schema to the local embedding identity. Seeding imports
each unique reference extract as one chunk through the existing embedding/import
pipeline. Content-based fingerprints make a repeated seed idempotent. Seeding
refuses a database containing unrelated financial chunks and never deletes rows.
The confirmation flag declares that the configured database is disposable.
Use the same dataset and model identity for the API and seed process.

The 18 cases include complete category extracts, refunds, transfers, budgeting,
possible duplicate charges, month comparisons, missing evidence, conditional
hypothetical trades and P&L. Complete category extracts deliberately provide
bounded evidence for totals. Top-k semantic results alone cannot establish a
complete statement total. These fixtures do not benchmark raw CSV parsing.

## 3. Start the API and run an unjudged benchmark

In the configured terminal, start the API.

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
```

In a second terminal, activate the same virtual environment and set `API_KEY` to
the same value. The evaluation CLI reads credentials from process environment
variables, not directly from `.env`.

```powershell
$env:API_KEY = 'fictional-eval-api-key-change-me-1234567890'
python -m eval.eval_harness --output eval/reports/capture.json
```

The harness defaults to `local_only` and fresh conversation/request IDs for every
case and every run. No prior conversation is used as hidden evidence. Requests
run sequentially. There are no automatic chat retries that could create ambiguous
continuations. Agent `failed` and `limit_reached` responses are recorded and make
the process fail even when HTTP returns 200.

Use `--case dining_total` to select a case, repeat `--case` to select several, and
use `--dataset path.json` for a versioned synthetic dataset with the same schema.
A run evaluates the selected subset in dataset order. Seeding always imports the
full selected dataset's reference pool, retaining distractors for retrieval tests.

## 4. Enable an LLM judge

GPT-4o structured grading is supported through the Responses API. The default
pinned judge is `gpt-4o-2024-08-06`. Configure a model available to your account
explicitly if that snapshot is unavailable. No provider/model is substituted after
a request fails. Structured Outputs constrains the result format and application
validation rejects missing, nonfinite or out-of-range grades. It does not establish
that a judge's interpretation is correct. [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)

```powershell
$env:EVAL_JUDGE_API_KEY = '<your OpenAI key>'
python -m eval.eval_harness --judge openai --allow-cloud-judge --judge-model gpt-4o-2024-08-06 --output eval/reports/judged.json
```

Anthropic uses a forced `submit_grade` tool with the same validated score schema.
Select an available model explicitly. Claude 3.5 Sonnet retired on October 28,
2025, so the harness rejects its retired IDs. [Anthropic model deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations)

```powershell
$env:EVAL_JUDGE_API_KEY = '<your Anthropic key>'
$env:EVAL_JUDGE_MODEL = '<available model ID from your account>'
python -m eval.eval_harness --judge anthropic --allow-cloud-judge --judge-model $env:EVAL_JUDGE_MODEL --output eval/reports/anthropic.json
```

A fully judged run makes two judge requests per valid agent response. Temperature
is zero, SDK retries are disabled, and failures remain explicit. OpenAI requests
use `store=false`. Provider retention follows your account's policy. The judge
has no executable tools. Its submission tool is a structured grading interface.

## 5. Interpret the report

Every case records HTTP/agent status, answer, actual tool trace, retrieved chunks,
exact retrieval metrics, claim assessments, judge justifications and timings.
Incomplete agent responses can still be graded for diagnosis, and retain their
incomplete status. An interrupted run preserves completed case records, with a
null `finished_at`. Reports are replaced atomically after each case.

| Metric | Definition |
| --- | --- |
| Retrieval precision | Exact matched reference chunks divided by distinct retrieved chunks across the entire turn |
| Retrieval recall | Matched required chunks divided by all required reference chunks |
| Retrieval hit | Whether at least one required reference chunk was retrieved |
| All references retrieved | Whether every required reference was retrieved |
| Retrieval relevance | Judge assessment from 0 to 1 of retrieved chunks' relevance |
| Faithfulness | Supported factual claims divided by all judged factual claims |
| Hallucination rate | Unsupported factual claims divided by all judged factual claims |
| Answer correctness | Judge score from 0 to 1 against the golden target, including calculations and uncertainty |
| Claim-weighted hallucination rate | Unsupported claims across scored cases divided by all judged claims |

Exact matching normalizes whitespace only, preserving dates, signs, amounts and
wording. Repeated chunks across tool calls are deduplicated by normalized content.
Precision/recall therefore measure a turn's union of results, not precision@k of
a single search. Lexically different but useful evidence can receive good semantic
relevance with low exact recall.

Groundedness is judged against actual tool observations without the golden answer
or reference chunks. Correctness is a separate judge request with the golden
reference. A correct answer based on unretrieved evidence can still be ungrounded.
Calculations entailed by retrieved facts count as supported. An answer containing
no verifiable claims receives null faithfulness and hallucination rate. Empty
reference sets on absence questions yield null exact retrieval metrics. An empty
retrieval on a positive-reference case yields zero precision/recall and no hit.

Aggregate scores are macro averages over available values. Each metric includes
its denominator as a `_count` field. API/judge failures remain null for missing
scores and increment error counts. Completion rate and incomplete counts expose
agent failures independently of judge quality. Averages alone can hide missing
coverage, so inspect these counts with each score. Latency uses nearest-rank p95
and includes failed API requests. Judge latency is recorded separately.

## 6. Track changes and gate CI

Keep the dataset bytes, selected cases, routing mode and judge configuration
identical for paired runs. Record source revision, local model digest, Ollama
configuration and hardware alongside each experiment. The report includes the
dataset SHA-256, protocol version, SDK versions and actual routing events from
each response. A baseline comparison checks the dataset hash, case selection,
protocol and judge/routing configuration before any chat requests.

```powershell
python -m eval.eval_harness --judge openai --allow-cloud-judge --baseline eval/reports/judged.json --output eval/reports/candidate.json --min-faithfulness 0.90 --min-retrieval-hit 0.90 --min-correctness 0.85
```

`baseline_deltas` reports candidate minus baseline for numeric aggregates. Positive
hallucination-rate and latency deltas indicate increases. Scores can vary between
judge runs despite fixed temperature. Review claim-level judgments against the
trace and calibrate with human-labeled examples before treating small differences
as meaningful. A higher score on these 18 cases does not establish broad financial
accuracy.

The CLI exits 0 when requests complete and configured thresholds pass, 1 for case
errors, incomplete agent runs or threshold failures, 2 for setup errors, and 130
when interrupted. Missing required metric values fail a configured threshold.
Capture-only runs do not support faithfulness/correctness thresholds. Reports
contain no invented judge scores when judging is disabled or fails.

## 7. Run regression tests and clean up

```powershell
python -m pytest tests/test_eval.py -q
python -m pytest -q
```

Tests use mock HTTP providers and scripted agent responses. They check retrieval
math, null coverage, cloud opt-in, evidence isolation, malformed grades, HTTP and
agent failures, offline preflight, report persistence and baseline compatibility.
Set `TEST_DATABASE_URL` to an initialized disposable database to include the
repository's persistence tests.

Stop the evaluation API when done. Remove only the dedicated container created
in step 2 if its data is no longer needed.

```powershell
docker rm --force finbrain-eval-db
```


### Validation results

The full suite passed with 119 tests on Python 3.11 in Docker against PostgreSQL
16/pgvector. This includes 22 Phase 5 tests. `pip check` reported no broken
requirements. OpenAI and Anthropic judge contracts were exercised with mocked
HTTP responses. Live cloud grading was not run.

A live local smoke test initialized the isolated fixture database and imported
11 reference chunks. A second seed inserted zero and skipped all 11. The dining
case retrieved its required reference, with recall 1.0 and union precision 0.10.
The Llama 3.1 8B local model repeated retrieval until the configured four-step cap.
The harness saved the actual trace, reported `limit_reached`, left unrequested
judge scores null, and exited 1. These results validate failure reporting and
retrieval plumbing without establishing successful end-to-end answer synthesis.
The generated smoke report is kept locally under `eval/reports/local-smoke.json`.
