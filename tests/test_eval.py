"""Evaluation metric, privacy, wire-contract and failure-reporting regressions."""
import argparse
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from agent.trading_agent import ChatResponse
from eval.eval_harness import (
    DEFAULT_DATASET, GoldenCase, aggregate, atomic_report, check_baseline,
    evaluate_case, extract_sources, load_dataset, retrieval_metrics, run, seed_fixtures,
)
from eval.judge import CorrectnessGrade, GroundingGrade, Judge, JudgeConfig, JudgeError


@pytest.fixture
def anyio_backend():
    return "asyncio"


def case(refs=None):
    return GoldenCase(id="fixture", kind="finance", question="What did dining cost?",
                      ground_truth_context=["Dining USD 10.00"] if refs is None else refs,
                      expected_answer="USD 10.00")


def response(request_id="00000000-0000-0000-0000-000000000001", conversation_id="eval", status="completed"):
    return ChatResponse(
        request_id=UUID(request_id), conversation_id=conversation_id, status=status,
        answer="Dining cost USD 10.00.", started_at="2025-01-01T00:00:00Z",
        completed_at="2025-01-01T00:00:01Z", portfolio=None,
        steps=[{
            "call_id": "r1", "tool": "search_financial_kb", "arguments": {"query": "dining"},
            "started_at": "2025-01-01T00:00:00Z", "status": "succeeded",
            "observation": {"ok": True, "data": {"results": [
                {"id": "chunk-1", "content": "Dining USD 10.00", "metadata": {"source": "synthetic"}},
            ]}},
        }],
    )


def test_golden_dataset_has_18_fixed_synthetic_cases():
    dataset, digest = load_dataset(DEFAULT_DATASET)
    assert len(dataset.cases) == 18 and dataset.synthetic
    assert len(digest) == 64
    assert {item.kind for item in dataset.cases} == {"finance", "trading", "abstention"}
    assert len({text for item in dataset.cases for text in item.ground_truth_context}) == 11
    assert all("Do not execute" in item.question for item in dataset.cases if item.kind == "trading")


def test_retrieval_union_deduplicates_and_preserves_exact_amounts():
    result = response()
    result.steps.append(result.steps[0].model_copy(deep=True))
    hits = result.steps[0].observation["data"]["results"]
    hits.append({"id": "chunk-2", "content": "Dining USD 100.00", "metadata": {}})
    sources = extract_sources(result)
    metrics = retrieval_metrics(case(["Dining\n USD 10.00", "Missing reference"]), sources)
    assert len(sources) == 2
    assert metrics["retrieval_precision"] == 0.5
    assert metrics["retrieval_recall"] == 0.5
    assert metrics["retrieval_hit"] is True
    assert metrics["all_references_retrieved"] is False


def test_missing_sources_and_absence_metrics_are_distinct():
    assert retrieval_metrics(case(), [])["retrieval_recall"] == 0
    assert retrieval_metrics(case(), [])["retrieval_hit"] is False
    assert retrieval_metrics(case([]), [])["retrieval_recall"] is None
    assert retrieval_metrics(case([]), [])["retrieval_hit"] is None


def test_failed_retrieval_is_not_evidence_and_malformed_success_fails():
    result = response()
    result.steps[0].status = "error"
    assert extract_sources(result) == []
    result.steps[0].status = "succeeded"
    result.steps[0].observation["data"]["results"] = [{"content": "missing metadata"}]
    with pytest.raises(ValueError, match="Malformed"):
        extract_sources(result)


def test_judge_opt_in_and_retired_model_guards():
    with pytest.raises(ValidationError):
        JudgeConfig(provider="openai", api_key="secret")
    with pytest.raises(ValidationError):
        JudgeConfig(provider="openai", allow_cloud=True)
    with pytest.raises(ValidationError):
        JudgeConfig(provider="anthropic", model="claude-3-5-sonnet-20241022",
                    allow_cloud=True, api_key="secret")
    config = JudgeConfig(provider="openai", allow_cloud=True, api_key="secret")
    assert "secret" not in repr(config) and "secret" not in config.model_dump_json()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, "0.9", True])
def test_judge_rejects_invalid_scores(value):
    with pytest.raises(ValidationError):
        CorrectnessGrade(score=value, reason="reason")


@pytest.mark.anyio
async def test_grounding_reference_isolation_and_claim_math():
    class ScriptedJudge(Judge):
        def __init__(self):
            super().__init__(JudgeConfig())
            self.payloads = []

        async def grade(self, schema, instructions, payload):
            self.payloads.append(payload)
            if schema is GroundingGrade:
                return GroundingGrade(claims=[
                    {"claim": "Dining cost 10", "supported": True, "reason": "Observed"},
                    {"claim": "Rent cost 500", "supported": False, "reason": "Not observed"},
                ], retrieval_relevance=1.0, reason="Relevant dining evidence")
            return CorrectnessGrade(score=0.5, reason="Extra unsupported claim")

    judge = ScriptedJudge()
    selected = case(["SECRET_GOLDEN_CONTEXT"])
    selected.expected_answer = "SECRET_GOLDEN_ANSWER"
    metrics = await judge.evaluate(selected, response(), extract_sources(response()))
    assert metrics["faithfulness"] == metrics["hallucination_rate"] == 0.5
    assert "SECRET_GOLDEN" not in json.dumps(judge.payloads[0])
    assert "SECRET_GOLDEN_ANSWER" in json.dumps(judge.payloads[1])
    assert "tool_evidence" in judge.payloads[0]


@pytest.mark.anyio
async def test_no_claims_does_not_earn_perfect_faithfulness(monkeypatch):
    judge = Judge(JudgeConfig())
    async def grade(schema, *args):
        if schema is GroundingGrade:
            return GroundingGrade(claims=[], retrieval_relevance=None, reason="Abstains")
        return CorrectnessGrade(score=1.0, reason="Correct abstention")
    monkeypatch.setattr(judge, "grade", grade)
    metrics = await judge.evaluate(case([]), response(), [])
    assert metrics["faithfulness"] is None and metrics["hallucination_rate"] is None
    assert metrics["claim_count"] == 0


@pytest.mark.anyio
async def test_agent_capture_uses_unique_request_identity_and_null_judge_scores():
    payloads = []
    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(200, json=response(payload["request_id"], payload["conversation_id"]).model_dump(mode="json"))
    async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        judge = Judge(JudgeConfig())
        first = await evaluate_case(client, case(), judge, "local_only", "run1")
        second = await evaluate_case(client, case(), judge, "local_only", "run2")
    assert first["status"] == "unjudged" and first["metrics"]["retrieval_hit"]
    assert first["request_id"] != second["request_id"]
    assert first["conversation_id"] != second["conversation_id"]
    assert payloads[0]["routing_mode"] == "local_only"
    summary = aggregate([first, second])
    assert summary["faithfulness"] is None and summary["faithfulness_count"] == 0


@pytest.mark.anyio
async def test_http_and_incomplete_agent_failures_remain_visible():
    async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text="SECRET_ERROR_BODY"))) as client:
        failed = await evaluate_case(client, case(), Judge(JudgeConfig()), "local_only", "r")
    assert failed["status"] == "error" and "SECRET_ERROR_BODY" not in json.dumps(failed)
    def handler(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json=response(payload["request_id"], payload["conversation_id"], "limit_reached").model_dump(mode="json"))
    async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        incomplete = await evaluate_case(client, case(), Judge(JudgeConfig()), "local_only", "r")
    summary = aggregate([failed, incomplete])
    assert summary["error_count"] == summary["incomplete_count"] == 1
    assert summary["completion_rate"] == 0


@pytest.mark.anyio
async def test_judge_failure_retains_retrieval_metrics():
    class BrokenJudge(Judge):
        async def evaluate(self, *args):
            raise JudgeError("Judge response was incomplete or refused")
    def handler(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json=response(payload["request_id"], payload["conversation_id"]).model_dump(mode="json"))
    async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        row = await evaluate_case(client, case(), BrokenJudge(JudgeConfig(
            provider="openai", api_key="fake-key", allow_cloud=True)), "local_only", "run")
    assert row["status"] == "error" and row["metrics"]["retrieval_recall"] == 1
    assert "faithfulness" not in row["metrics"]


@pytest.mark.anyio
async def test_openai_judge_structured_wire_contract(monkeypatch):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        assert str(request.url) == "https://api.openai.com/v1/responses"
        return httpx.Response(200, json={
            "id": "resp_grade", "object": "response", "created_at": 1,
            "status": "completed", "model": "gpt-4o-2024-08-06",
            "output": [{"id": "msg_grade", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": '{"score":0.75,"reason":"Mostly correct"}', "annotations": []}]}],
        })
    real_client = httpx.AsyncClient
    monkeypatch.setattr("eval.judge.AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    judge = Judge(JudgeConfig(provider="openai", allow_cloud=True, api_key="fake-key"))
    grade = await judge.grade(CorrectnessGrade, "Judge", {"candidate": "fictional"})
    assert grade.score == 0.75 and captured[0]["store"] is False
    assert captured[0]["text"]["format"]["type"] == "json_schema"
    assert captured[0]["text"]["format"]["strict"] is True


@pytest.mark.anyio
async def test_anthropic_judge_tool_contract_and_invalid_grade(monkeypatch):
    captured = []
    invalid = False
    def handler(request):
        captured.append(json.loads(request.content))
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        return httpx.Response(200, json={
            "id": "msg_grade", "type": "message", "role": "assistant", "model": "selected-model",
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "content": [{"type": "tool_use", "id": "grade1", "name": "submit_grade",
                        "input": {"score": 99 if invalid else 1.0, "reason": "Correct"}}],
        })
    real_client = httpx.AsyncClient
    monkeypatch.setattr("eval.judge.AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    judge = Judge(JudgeConfig(provider="anthropic", model="selected-model", allow_cloud=True, api_key="fake-key"))
    assert (await judge.grade(CorrectnessGrade, "Judge", {})).score == 1
    assert captured[0]["tool_choice"] == {"type": "tool", "name": "submit_grade"}
    invalid = True
    with pytest.raises(JudgeError, match="validation failed"):
        await judge.grade(CorrectnessGrade, "Judge", {})


def test_atomic_report_and_baseline_compatibility(tmp_path):
    report = {"protocol_version": "v1", "dataset_sha256": "digest", "selected_case_ids": ["a"],
              "configuration": {"judge_model": "fixed"}, "aggregate": {},
              "finished_at": "2025-01-01T00:00:00Z", "results": [{"case_id": "a"}]}
    path = tmp_path / "report.json"
    atomic_report(path, report)
    assert check_baseline(path, report) == report
    assert list(tmp_path.glob("*.tmp")) == []
    with pytest.raises(ValueError):
        check_baseline(path, dict(report, dataset_sha256="changed"))
    atomic_report(path, dict(report, finished_at=None))
    with pytest.raises(ValueError, match="completed run"):
        check_baseline(path, report)


def test_seed_requires_confirmation_before_database_access():
    dataset, _ = load_dataset(DEFAULT_DATASET)
    with pytest.raises(ValueError, match="confirm-disposable"):
        seed_fixtures(dataset, False)


@pytest.mark.anyio
async def test_run_requires_offline_preflight_before_chat(monkeypatch, tmp_path):
    captured = []
    def handler(request):
        captured.append(request.url.path)
        return httpx.Response(200, json={"offline_mode": False})
    real_client = httpx.AsyncClient
    monkeypatch.setattr("eval.eval_harness.AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    dataset, digest = load_dataset(DEFAULT_DATASET)
    args = argparse.Namespace(baseline=None, output=tmp_path / "report.json", routing_mode="local_only",
                              base_url="http://localhost", api_key="fake-key", timeout=1)
    with pytest.raises(ValueError, match="LLM_OFFLINE_MODE"):
        await run(args, dataset, digest, dataset.cases[:1], JudgeConfig())
    assert captured == ["/api/v1/llm/status"]


@pytest.mark.anyio
async def test_run_report_thresholds_and_incomplete_exit(monkeypatch, tmp_path):
    def handler(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"offline_mode": True})
        payload = json.loads(request.content)
        return httpx.Response(200, json=response(payload["request_id"], payload["conversation_id"], "limit_reached").model_dump(mode="json"))
    real_client = httpx.AsyncClient
    monkeypatch.setattr("eval.eval_harness.AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    dataset, digest = load_dataset(DEFAULT_DATASET)
    args = argparse.Namespace(baseline=None, output=tmp_path / "report.json", routing_mode="local_only",
                              base_url="http://localhost", api_key="SECRET_KEY", timeout=1,
                              min_faithfulness=None, min_retrieval_hit=0.9, min_correctness=None)
    assert await run(args, dataset, digest, dataset.cases[:1], JudgeConfig()) == 1
    report = json.loads(args.output.read_text())
    assert report["finished_at"] and report["threshold_failures"] == ["retrieval_hit"]
    assert "SECRET_KEY" not in args.output.read_text()
