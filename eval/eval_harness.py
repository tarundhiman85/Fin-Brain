"""Evaluate the live agent using fixed fixtures and optional cloud LLM judging.

Run from the repository root with python -m eval.eval_harness --help.
"""
import argparse
import asyncio
import hashlib
import json
import os
import statistics
import tempfile
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.trading_agent import ChatResponse
from eval.judge import Judge, JudgeConfig, JudgeError

DEFAULT_DATASET = Path(__file__).with_name("golden_finance_dataset.json")
PROTOCOL_VERSION = "finbrain-eval-v1"


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    kind: Literal["finance", "abstention", "trading"]
    question: str = Field(min_length=1, max_length=8000)
    ground_truth_context: list[str] = Field(max_length=50)
    expected_answer: str = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_evidence(self):
        normalized = [normalize(text) for text in self.ground_truth_context]
        if any(not text for text in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("Reference contexts must be distinct nonblank chunks")
        return self


class GoldenDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    dataset_id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    synthetic: Literal[True]
    as_of: str
    currency: str
    description: str
    cases: list[GoldenCase] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case IDs")
        return self


def normalize(text: str) -> str:
    """Ignore only whitespace. Dates, signs, case and amounts remain exact."""
    return " ".join(text.split())


def load_dataset(path: Path):
    raw = path.read_bytes()
    return GoldenDataset.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def extract_sources(response: ChatResponse) -> list[dict]:
    unique = {}
    for step in response.steps:
        observation = step.observation or {}
        if step.tool != "search_financial_kb" or step.status != "succeeded" or observation.get("ok") is not True:
            continue
        data = observation.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("Malformed retrieval observation")
        for hit in data["results"]:
            if (not isinstance(hit, dict) or not isinstance(hit.get("content"), str)
                    or not hit["content"].strip() or not isinstance(hit.get("metadata"), dict)
                    or not isinstance(hit.get("id"), str)):
                raise ValueError("Malformed retrieved source")
            key = normalize(hit["content"])
            unique.setdefault(key, {"id": hit["id"], "content": hit["content"], "metadata": hit["metadata"]})
    return list(unique.values())


def retrieval_metrics(case: GoldenCase, sources: list[dict]) -> dict:
    expected = {normalize(text) for text in case.ground_truth_context}
    actual = {normalize(hit["content"]) for hit in sources}
    matched = expected & actual
    # Absence questions have no required reference. Similarity search returning
    # distractors must not be interpreted as proof of zero expenses.
    return {
        "reference_count": len(expected), "retrieved_count": len(actual),
        "matched_reference_count": len(matched),
        "retrieval_precision": len(matched) / len(actual) if expected and actual else (0.0 if expected else None),
        "retrieval_recall": len(matched) / len(expected) if expected else None,
        "retrieval_hit": bool(matched) if expected else None,
        "all_references_retrieved": matched == expected if expected else None,
    }


def atomic_report(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".finbrain-eval-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def aggregate(results: list[dict]) -> dict:
    summary = {"case_count": len(results)}
    for name in ("retrieval_precision", "retrieval_recall", "retrieval_hit",
                 "all_references_retrieved", "faithfulness", "hallucination_rate",
                 "answer_correctness", "retrieval_relevance"):
        values = [row["metrics"][name] for row in results if row["metrics"].get(name) is not None]
        summary[name] = statistics.mean(values) if values else None
        summary[name + "_count"] = len(values)
    summary["completed_count"] = sum(row.get("agent_status") == "completed" for row in results)
    summary["error_count"] = sum(row["status"] == "error" for row in results)
    summary["incomplete_count"] = sum(row.get("agent_status") in {"failed", "limit_reached"} for row in results)
    summary["completion_rate"] = summary["completed_count"] / len(results) if results else None
    claims = sum(row["metrics"].get("claim_count", 0) for row in results)
    unsupported = sum(row["metrics"].get("unsupported_claim_count", 0) for row in results)
    summary["claim_weighted_hallucination_rate"] = unsupported / claims if claims else None
    durations = sorted(row["agent_duration_ms"] for row in results if "agent_duration_ms" in row)
    summary["agent_latency_p50_ms"] = statistics.median(durations) if durations else None
    # Nearest-rank p95, including API failures. Report counts to avoid survivor bias.
    summary["agent_latency_p95_ms"] = durations[max(0, (95 * len(durations) + 99) // 100 - 1)] if durations else None
    return summary


async def evaluate_case(client, case, judge, routing_mode, run_id):
    request_id = str(uuid4())
    conversation_id = f"eval-{run_id}-{case.id}"
    row = {"case_id": case.id, "kind": case.kind, "question": case.question,
           "expected_answer": case.expected_answer, "ground_truth_context": case.ground_truth_context,
           "request_id": request_id, "conversation_id": conversation_id,
           "status": "error", "metrics": {}}
    started = perf_counter()
    try:
        result = await client.post("/api/v1/agent/chat", json={
            "message": case.question, "conversation_id": conversation_id,
            "request_id": request_id, "routing_mode": routing_mode,
        })
        row["agent_duration_ms"] = round((perf_counter() - started) * 1000, 3)
        row["http_status"] = result.status_code
        result.raise_for_status()
        response = ChatResponse.model_validate(result.json())
        if str(response.request_id) != request_id or response.conversation_id != conversation_id:
            raise ValueError("Response identity mismatch")
        response_payload = response.model_dump(mode="json")
        json.dumps(response_payload, allow_nan=False)
        row["agent_status"] = response.status
        row["response"] = response_payload
        sources = extract_sources(response)
        row["retrieved_sources"] = sources
        row["metrics"] = retrieval_metrics(case, sources)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        row.setdefault("agent_duration_ms", round((perf_counter() - started) * 1000, 3))
        row["error"] = "Agent HTTP request, response validation, or evidence extraction failed"
        return row
    row["status"] = "unjudged" if judge.config.provider == "none" else "scored"
    if judge.config.provider != "none":
        started = perf_counter()
        try:
            row["metrics"].update(await judge.evaluate(case, response, sources))
        except JudgeError as exc:
            row["status"], row["error"] = "error", str(exc)
        finally:
            row["judge_duration_ms"] = round((perf_counter() - started) * 1000, 3)
    return row


def seed_fixtures(dataset: GoldenDataset, confirmed: bool):
    """Import each unique reference as one chunk, matching retrieval exactly."""
    if not confirmed:
        raise ValueError("Seeding requires --confirm-disposable-database")
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from database import get_engine
    from embeddings import OllamaEmbedder
    from ingest_finance import ingest
    from models import FinancialDocumentChunk

    contexts = list(dict.fromkeys(text for case in dataset.cases for text in case.ground_truth_context))
    records = []
    for content in contexts:
        fingerprint = hashlib.sha256(f"{dataset.dataset_id}\0{content}".encode()).hexdigest()
        records.append({"content": content, "fingerprint": fingerprint,
                        "document_metadata": {"source_type": "financial_note",
                                              "source": dataset.dataset_id, "synthetic": True}})
    allowed = {record["fingerprint"] for record in records}
    with Session(get_engine()) as session:
        existing = set(session.scalars(select(FinancialDocumentChunk.fingerprint)))
    if existing - allowed:
        raise ValueError("Fixture database contains unrelated documents; use an isolated database")
    return ingest(records, OllamaEmbedder())


def check_baseline(path, report):
    baseline = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not baseline.get("finished_at")
            or [row.get("case_id") for row in baseline.get("results", [])] != report["selected_case_ids"]):
        raise ValueError("Baseline must be a completed run with the same case coverage")
    for key in ("protocol_version", "dataset_sha256", "selected_case_ids", "configuration"):
        if baseline.get(key) != report[key]:
            raise ValueError("Baseline dataset, selection, judge, or routing configuration differs")
    return baseline


async def run(args, dataset, digest, cases, config):
    run_id = uuid4().hex
    report = {
        "protocol_version": PROTOCOL_VERSION, "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
        "dataset_id": dataset.dataset_id, "dataset_sha256": digest,
        "selected_case_ids": [case.id for case in cases],
        "configuration": {"routing_mode": args.routing_mode, "judge_provider": config.provider,
                          "judge_model": config.model if config.provider != "none" else None},
        "versions": {name: version(name) for name in ("httpx", "pydantic", "openai", "anthropic")},
        "results": [], "aggregate": {},
    }
    baseline = check_baseline(args.baseline, report) if args.baseline else None
    output = args.output or Path("eval/reports") / f"{run_id}.json"
    judge = Judge(config)
    async with AsyncClient(
        base_url=args.base_url, headers={"X-API-Key": args.api_key},
        timeout=args.timeout, trust_env=False, follow_redirects=False,
    ) as client:
        # Fixed-scenario benchmarks require server-enforced offline execution.
        # Prompt instructions alone cannot prevent an erroneous model tool call.
        status = await client.get("/api/v1/llm/status")
        status.raise_for_status()
        server_status = status.json()
        if server_status.get("offline_mode") is not True:
            raise ValueError("Evaluation requires the isolated API to use LLM_OFFLINE_MODE=true")
        report["server_capabilities"] = server_status
        for case in cases:
            row = await evaluate_case(client, case, judge, args.routing_mode, run_id)
            report["results"].append(row)
            report["aggregate"] = aggregate(report["results"])
            atomic_report(output, report)
            print(f"{case.id} {row['status']} agent={row.get('agent_status', 'unavailable')}")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    if baseline:
        report["baseline_deltas"] = {
            name: value - baseline["aggregate"][name]
            for name, value in report["aggregate"].items()
            if isinstance(value, (int, float)) and isinstance(baseline.get("aggregate", {}).get(name), (int, float))
        }
    failures = []
    for name, minimum in (("faithfulness", args.min_faithfulness),
                          ("retrieval_hit", args.min_retrieval_hit),
                          ("answer_correctness", args.min_correctness)):
        value = report["aggregate"][name]
        if minimum is not None and (value is None or value < minimum):
            failures.append(name)
    report["threshold_failures"] = failures
    atomic_report(output, report)
    print(f"Report saved to {output}")
    return int(bool(failures or report["aggregate"]["error_count"] or report["aggregate"]["incomplete_count"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", action="append", default=[], dest="case_ids")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--routing-mode", choices=["auto", "local_only", "cloud_only"], default="local_only")
    parser.add_argument("--judge", choices=["none", "openai", "anthropic"], default="none")
    parser.add_argument("--judge-model")
    parser.add_argument("--allow-cloud-judge", action="store_true")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--judge-timeout", type=float, default=90)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--seed-fixtures", action="store_true")
    parser.add_argument("--confirm-disposable-database", action="store_true")
    for name in ("faithfulness", "retrieval-hit", "correctness"):
        parser.add_argument(f"--min-{name}", type=float)
    args = parser.parse_args()
    try:
        dataset, digest = load_dataset(args.dataset)
        if args.output and args.output.resolve() in {
                args.dataset.resolve(), args.baseline.resolve() if args.baseline else None}:
            raise ValueError("Output must differ from the dataset and baseline paths")
        unknown = set(args.case_ids) - {case.id for case in dataset.cases}
        if unknown:
            raise ValueError("Unknown case ID")
        cases = [case for case in dataset.cases if not args.case_ids or case.id in args.case_ids]
        if args.seed_fixtures:
            added, skipped = seed_fixtures(dataset, args.confirm_disposable_database)
            print(f"Fixture import added {added} chunks and skipped {skipped} existing chunks")
            return 0
        url = urlparse(args.base_url)
        if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
                or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError("Agent base URL must be a local HTTP origin")
        if not 0 < args.timeout <= 3600:
            raise ValueError("Timeout must be positive and at most 3600 seconds")
        for minimum in (args.min_faithfulness, args.min_retrieval_hit, args.min_correctness):
            if minimum is not None and not 0 <= minimum <= 1:
                raise ValueError("Thresholds must be between zero and one")
        if args.judge == "none" and (args.min_faithfulness is not None or args.min_correctness is not None):
            raise ValueError("Faithfulness/correctness thresholds require a judge")
        if args.judge == "anthropic" and not args.judge_model:
            raise ValueError("Anthropic requires an explicitly selected available --judge-model")
        config = JudgeConfig(provider=args.judge, model=args.judge_model or "gpt-4o-2024-08-06",
                             allow_cloud=args.allow_cloud_judge, api_key=os.getenv("EVAL_JUDGE_API_KEY", ""),
                             timeout=args.judge_timeout)
        args.api_key = os.getenv("API_KEY", "")
        if not args.api_key:
            raise ValueError("Set API_KEY in the process environment")
        return asyncio.run(run(args, dataset, digest, cases, config))
    except (ValueError, OSError):
        # Validation/OS errors may include paths, data or environment values.
        parser.exit(2, "Evaluation setup failed. Check dataset, local URL, API keys, explicit judge consent, model, baseline and disposable database flags.\n")
    except KeyboardInterrupt:
        parser.exit(130, "Evaluation interrupted. Completed cases remain in the report.\n")
    except Exception:
        parser.exit(2, "Evaluation failed. Check local database/model services and report storage.\n")


if __name__ == "__main__":
    raise SystemExit(main())
