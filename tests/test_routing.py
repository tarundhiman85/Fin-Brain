"""Routing privacy invariants, provider contracts, fallback and offline regressions."""
import json
import os
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")
os.environ.setdefault("API_KEY", "test-key-" + "x" * 40)

from agent.llm import ModelFailure, ModelReply
from config import Settings
from embeddings import EmbeddingError, OllamaEmbedder
from llm.cloud_client import CloudClient, public_projection
from llm.ollama_client import OllamaClient, OllamaUnavailable
from llm.policy import RoutingPolicyError, classify
from llm.router import HybridRouter, RoutingSession
from llm.schemas import PublicMarketContext
from llm.tools import RoutedToolsRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


def configured(**changes):
    values = {
        "agent_model": "local-test", "llm_cloud_enabled": True,
        "llm_cloud_model": "cloud-test", "llm_cloud_api_key": SecretStr("fake-provider-key"),
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


class Spy:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def complete(self, *args):
        self.calls.append(args)
        if self.failure:
            raise self.failure
        return ModelReply(items=[], calls=[], text="Public analysis.")


def session(settings, message, local=None, cloud=None, mode=None, **state_fields):
    state = {
        "history": [], "input": [{"role": "user", "content": message}],
        "routing_events": [], **state_fields,
    }
    decision = classify(message, settings, mode=mode)
    state["routing"] = decision.model_dump(mode="json")
    router = HybridRouter(settings, local=local or Spy(), cloud=cloud or Spy())
    return RoutingSession(router, state)


@pytest.mark.parametrize("message", [
    "Audit my bank statement and compare AAPL risk.",
    "Check our virtual cash and buy AAPL.",
    "Analyze salary and expenses before comparing NVDA and TSLA.",
    "Review transactions at merchant ABC.",
    "Contact me at private@example.com about AAPL.",
])
def test_private_queries_remain_local_in_auto(message):
    decision = classify(message, configured())
    assert decision.engine == "local" and decision.sensitive


def test_public_complexity_and_explicit_modes():
    settings = configured()
    assert classify("What is the AAPL quote?", settings).engine == "local"
    assert classify("Compare AAPL and MSFT market risk", settings).engine == "cloud"
    assert classify("Compare AAPL and MSFT risk", settings, mode="local_only").engine == "local"
    assert classify("AAPL stock quote", settings, mode="cloud_only").engine == "cloud"
    assert classify("Unrecognized request", settings).sensitive
    assert classify("Compare AAPL and MSFT risk", settings, history_private=True).engine == "local"


@pytest.mark.parametrize("settings,message", [
    (configured(), "Analyze my AAPL holdings."),
    (configured(llm_cloud_enabled=False), "AAPL stock quote"),
    (configured(llm_offline_mode=True), "Compare AAPL and MSFT risk"),
    (configured(llm_cloud_api_key=SecretStr("")), "AAPL stock quote"),
])
def test_cloud_only_cannot_override_privacy_or_server_settings(settings, message):
    with pytest.raises(RoutingPolicyError):
        classify(message, settings, mode="cloud_only")


def test_unapproved_explicit_public_context_is_rejected():
    with pytest.raises(RoutingPolicyError):
        classify("Compare public companies", configured(), public_context=PublicMarketContext(
            tickers=["AAPL", "UNAPPROVED"], analysis="comparison"))


def quote_items():
    return [
        {"type": "function_call", "name": "get_stock_quote", "call_id": "q1",
         "arguments": '{"ticker":"AAPL"}'},
        {"type": "function_call_output", "call_id": "q1", "output": json.dumps({
            "ok": True, "data": {
                "ticker": "AAPL", "currency": "USD", "price": "100.00",
                "as_of": "2026-11-09T12:00:00Z", "cash_balance": "SECRET_CASH",
                "merchant": "SECRET_MERCHANT", "content": "SECRET_DOCUMENT"}})},
    ]


@pytest.mark.anyio
async def test_cloud_receives_public_projection_never_raw_context():
    settings = configured()
    # The sentinel deliberately has no privacy keyword. Projection protects even a classifier miss.
    message = "Compare AAPL and MSFT market risk ZXQ-ZETA-ORCHID"
    remote = Spy()
    bound = session(settings, message, cloud=remote)
    messages = bound.state["input"] + [
        {"role": "assistant", "content": "SECRET_OLD_HISTORY"}] + quote_items()
    await bound.complete(messages, await _definitions(), "SECRET_SYSTEM_PERSONA")
    payload, definitions = remote.calls[0]
    serialized = json.dumps(payload)
    for secret in ("ZXQ-ZETA-ORCHID", "SECRET_OLD_HISTORY", "SECRET_CASH",
                   "SECRET_MERCHANT", "SECRET_DOCUMENT", "SECRET_SYSTEM_PERSONA"):
        assert secret not in serialized
    assert payload["tickers"] == ["AAPL", "MSFT"]
    assert payload["observed_quotes"][0]["price"] == "100.00"
    assert {tool["name"] for tool in definitions} == {"get_stock_quote"}
    assert bound.state["routing_events"][0]["duration_ms"] >= 0


async def _definitions():
    from agent.tools_registry import ToolsRegistry
    return await ToolsRegistry().definitions()


@pytest.mark.anyio
async def test_private_tool_observation_forces_sticky_local_downgrade():
    local, cloud = Spy(), Spy()
    bound = session(configured(), "Compare AAPL and MSFT risk", local, cloud)
    messages = bound.state["input"] + [
        {"type": "function_call", "name": "get_portfolio", "call_id": "private",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "private",
         "output": '{"ok":true,"data":{"cash_balance":"SECRET_BALANCE"}}'},
    ]
    await bound.complete(messages, await _definitions(), "system")
    assert len(local.calls) == 1 and cloud.calls == []
    assert bound.decision.conversation_private and bound.decision.engine == "local"


@pytest.mark.anyio
async def test_private_outage_has_no_cloud_fallback():
    local, cloud = Spy(OllamaUnavailable("offline")), Spy()
    bound = session(configured(llm_public_cloud_fallback=True), "Audit my bank statement.", local, cloud)
    with pytest.raises(OllamaUnavailable):
        await bound.complete(bound.state["input"], [], "system")
    assert len(local.calls) == 1 and cloud.calls == []


@pytest.mark.anyio
async def test_public_fallback_requires_opt_in_and_local_only_stays_local():
    settings = configured(llm_public_cloud_fallback=True)
    local, cloud = Spy(OllamaUnavailable("offline")), Spy()
    bound = session(settings, "AAPL stock quote", local, cloud)
    await bound.complete(bound.state["input"], await _definitions(), "system")
    assert len(cloud.calls) == 1 and bound.decision.engine == "cloud"
    bound = session(settings, "AAPL stock quote", Spy(OllamaUnavailable("offline")), Spy(), mode="local_only")
    with pytest.raises(OllamaUnavailable):
        await bound.complete(bound.state["input"], [], "system")
    assert bound.router.cloud.calls == []


@pytest.mark.anyio
async def test_cloud_failure_can_fall_back_locally_in_auto():
    bound = session(configured(), "Compare AAPL and MSFT risk", Spy(), Spy(ModelFailure("down")))
    await bound.complete(bound.state["input"], await _definitions(), "system")
    assert [event["engine"] for event in bound.state["routing_events"]] == ["cloud", "local"]
    assert bound.decision.engine == "local"


@pytest.mark.anyio
async def test_cloud_registry_cannot_read_finances_or_execute_trades():
    from agent.tools_registry import ToolsRegistry
    bound = session(configured(), "Compare AAPL and MSFT risk")
    registry = RoutedToolsRegistry(ToolsRegistry(), bound)
    assert {tool["name"] for tool in await registry.definitions()} == {"get_stock_quote"}
    for name in ("get_portfolio", "search_financial_kb", "execute_paper_trade"):
        assert not (await registry.invoke(name, {}, uuid4()))["ok"]
    assert not (await registry.invoke("get_stock_quote", {"ticker": "TSLA"}, uuid4()))["ok"]


@pytest.mark.anyio
async def test_local_backup_never_reuses_legacy_remote_endpoint(monkeypatch):
    seen = []
    settings = configured(
        llm_local_model="primary", llm_local_fallback_model="backup",
        agent_llm_base_url="https://api.openai.com/v1", agent_allow_remote_llm=True)
    client = OllamaClient(settings)
    async def verify(name):
        seen.append(name)
    async def complete(model, messages, tools, instructions):
        assert model.settings.agent_llm_base_url == settings.ollama_url + "/v1"
        assert not model.settings.agent_allow_remote_llm
        if model.settings.agent_model == "primary":
            raise ModelFailure("model down")
        return ModelReply(items=[], calls=[], text="local")
    monkeypatch.setattr(client, "verify_model", verify)
    monkeypatch.setattr("llm.ollama_client.ToolCallingModel.complete", complete)
    assert await client.generate("private") == "local"
    assert seen == ["primary", "backup"]


def test_embeddings_refuse_cloud_backed_model_before_sending_text(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"remote_host": "https://ollama.com", "remote_model": "remote"})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(EmbeddingError):
        OllamaEmbedder().embed(["SECRET_BANK_TEXT"])
    assert [request.url.path for request in requests] == ["/api/show"]
    assert b"SECRET_BANK_TEXT" not in requests[0].content


@pytest.mark.anyio
async def test_anthropic_tool_use_adapter_uses_public_payload(monkeypatch):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "cloud-test",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "get_stock_quote",
                         "input": {"ticker": "AAPL"}}],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
    real_client = httpx.AsyncClient
    monkeypatch.setattr("llm.cloud_client.AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    reply = await CloudClient(configured(llm_cloud_provider="anthropic")).complete(
        {"task": "public", "tickers": ["AAPL"]}, await _definitions())
    assert reply.calls[0]["call_id"] == "toolu_1"
    assert captured[0]["tools"][0]["input_schema"]["type"] == "object"


def test_offline_guard_precedes_yfinance_import(monkeypatch):
    import trading_service
    monkeypatch.setattr(trading_service, "get_settings", lambda: configured(llm_offline_mode=True))
    with pytest.raises(trading_service.TradingError, match="LLM_OFFLINE_MODE"):
        trading_service.fetch_stock_quote("AAPL")

@pytest.mark.anyio
async def test_openai_adapter_drops_raw_task_and_extra_private_fields(monkeypatch):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_public", "object": "response", "created_at": 1,
            "status": "completed", "model": "cloud-test",
            "output": [{"id": "fc_public", "type": "function_call", "call_id": "q1",
                        "name": "get_stock_quote", "arguments": '{"ticker":"AAPL"}',
                        "status": "completed"}],
        })
    real_client = httpx.AsyncClient
    monkeypatch.setattr("agent.llm.AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    definitions = await _definitions()
    for tool in definitions:
        tool["description"] = "SECRET_TOOL_DESCRIPTION"
        tool["parameters"]["title"] = "SECRET_TOOL_SCHEMA"
    await CloudClient(configured()).complete(
        {"task": "SECRET_TASK_TEXT", "tickers": ["AAPL"], "balance": "SECRET_BALANCE",
         "analysis": "risk", "drivers": ["interest_rates"]}, definitions)
    body = json.dumps(captured[0])
    assert "SECRET_TASK_TEXT" not in body and "SECRET_BALANCE" not in body
    assert "SECRET_TOOL_DESCRIPTION" not in body and "SECRET_TOOL_SCHEMA" not in body
    assert "interest_rates" in body and captured[0]["store"] is False
    assert {tool["name"] for tool in captured[0]["tools"]} == {"get_stock_quote"}


def test_routing_preview_and_status_inherit_authentication(monkeypatch):
    from fastapi.testclient import TestClient
    from config import get_settings
    from main import app
    monkeypatch.setattr("main_router_middleware.get_settings", configured)
    with TestClient(app) as api:
        assert api.get("/api/v1/llm/status").status_code == 401
        headers = {"X-API-Key": get_settings().api_key.get_secret_value()}
        status = api.get("/api/v1/llm/status", headers=headers)
        assert status.status_code == 200 and "fake-provider-key" not in status.text
        public = api.post("/api/v1/llm/route", headers=headers,
                          json={"message": "Compare AAPL and MSFT risk"})
        assert public.status_code == 200 and public.json()["engine"] == "cloud"
        private = api.post("/api/v1/llm/route", headers=headers,
                           json={"message": "My bank statement", "routing_mode": "cloud_only"})
        assert private.status_code == 403

@pytest.mark.anyio
async def test_public_capabilities_never_escalate_after_private_context_downgrade():
    from agent.tools_registry import ToolsRegistry
    bound = session(configured(), "Compare AAPL and MSFT risk", Spy(), Spy())
    registry = RoutedToolsRegistry(ToolsRegistry(), bound)
    messages = bound.state["input"] + [{
        "type": "function_call", "name": "search_financial_kb",
        "call_id": "forbidden", "arguments": '{"query":"private"}'}]
    await bound.complete(messages, await registry.definitions(), "system")
    assert bound.decision.conversation_private and bound.decision.engine == "local"
    assert {tool["name"] for tool in await registry.definitions()} == {"get_stock_quote"}
    assert not (await registry.invoke("execute_paper_trade", {}, uuid4()))["ok"]


@pytest.mark.anyio
async def test_cloud_only_marks_discovered_private_context_before_blocking():
    bound = session(configured(), "AAPL stock quote", mode="cloud_only")
    messages = bound.state["input"] + [{
        "type": "function_call", "name": "get_portfolio", "call_id": "private", "arguments": "{}"}]
    with pytest.raises(ModelFailure):
        await bound.complete(messages, [], "system")
    assert bound.state["routing"]["conversation_private"]
    assert bound.router.cloud.calls == []
