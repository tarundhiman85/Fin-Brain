"""Responses protocol and registry validation without a paid model service."""
import json
import os
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError, SecretStr

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")
os.environ.setdefault("API_KEY", "test-key-" + "x" * 40)

from agent.llm import ModelConfigurationError, ModelFailure, ToolCallingModel
from agent.tools_registry import ToolsRegistry, TradeArguments
from agent.trading_agent import ChatRequest
from config import Settings
from tests.test_phase2_mcp import quote
from models import VirtualPortfolio
from trading_service import TradingError, apply_fill


@pytest.fixture
def anyio_backend():
    return "asyncio"


def settings(**updates):
    return Settings(_env_file=None, agent_model="mock-model", agent_local_json_mode=False, **updates)


def test_remote_model_requires_explicit_opt_in():
    with pytest.raises(ModelConfigurationError):
        ToolCallingModel(settings(agent_llm_base_url="https://api.openai.com/v1"))
    model = ToolCallingModel(settings(
        agent_llm_base_url="https://api.openai.com/v1", agent_allow_remote_llm=True,
        agent_llm_api_key=SecretStr("fake-test-key")))
    assert not model.local
    with pytest.raises(ModelConfigurationError):
        ToolCallingModel(settings(agent_llm_base_url="http://remote.example/v1",
                                agent_allow_remote_llm=True))


@pytest.mark.anyio
async def test_real_sdk_function_call_protocol(monkeypatch):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 1,
            "status": "completed", "model": "mock-model",
            "output": [{"id": "fc_1", "type": "function_call", "call_id": "call_1",
                        "name": "get_stock_quote", "arguments": '{"ticker":"AAPL"}',
                        "status": "completed"}],
        })
    real_client = httpx.AsyncClient
    monkeypatch.setattr("agent.llm.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    reply = await ToolCallingModel(settings()).complete(
        [{"role": "user", "content": "Apple quote"}], [], "test instructions")
    assert reply.calls[0]["call_id"] == "call_1"
    assert requests[0]["store"] is False and requests[0]["parallel_tool_calls"] is False
    assert requests[0]["instructions"] == "test instructions"


@pytest.mark.anyio
async def test_incomplete_model_reply_does_not_expose_calls(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 1,
            "status": "incomplete", "model": "mock-model", "output": []})
    real_client = httpx.AsyncClient
    monkeypatch.setattr("agent.llm.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ModelFailure, match="incomplete"):
        await ToolCallingModel(settings()).complete([], [], "test")


@pytest.mark.anyio
async def test_registry_discovers_strict_tools_and_rejects_unknown():
    registry = ToolsRegistry()
    definitions = await registry.definitions()
    assert {item["name"] for item in definitions} == {
        "search_financial_kb", "get_stock_quote", "get_portfolio", "execute_paper_trade"}
    for item in definitions:
        schema = item["parameters"]
        assert item["strict"] and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
    assert (await registry.invoke("shell", {"command": "anything"}, uuid4()))["ok"] is False
    assert (await registry.invoke("get_portfolio", {"unexpected": True}, uuid4()))["ok"] is False


def test_chat_and_trade_input_validation():
    with pytest.raises(ValidationError):
        ChatRequest(message=" ", conversation_id="test")
    with pytest.raises(ValidationError):
        TradeArguments(ticker="AAPL", shares=True, order_type="BUY")


@pytest.mark.parametrize("limits", [
    {"max_buy_price": Decimal("99")}, {"max_notional": Decimal("199")},
])
def test_fill_limits_apply_before_ledger_mutation(limits):
    from unittest.mock import Mock
    portfolio = VirtualPortfolio(id=1, cash_balance=Decimal("1000"), currency="USD")
    session = Mock()
    with pytest.raises(TradingError):
        apply_fill(session, portfolio, None, "AAPL", Decimal("2"), "BUY", quote(), uuid4(), **limits)
    assert portfolio.cash_balance == Decimal("1000")
    session.add.assert_not_called()

def test_local_model_numeric_strings_are_validated_without_boolean_coercion():
    args = TradeArguments.model_validate({
        "ticker": "AAPL", "shares": "1", "order_type": "BUY",
        "max_buy_price": "null", "min_sell_price": "null", "max_notional": "400"})
    assert args.shares == 1 and args.max_buy_price is None and args.max_notional == 400
    with pytest.raises(ValidationError):
        TradeArguments(ticker="AAPL", shares="NaN", order_type="BUY")
    with pytest.raises(ValidationError):
        TradeArguments(ticker="AAPL", shares=1, order_type="BUY", max_notional=True)

@pytest.mark.anyio
async def test_schema_constrained_local_decision_protocol(monkeypatch):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chat_test", "object": "chat.completion", "created": 1, "model": "local",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({
                    "tool": "get_portfolio", "arguments": {}, "answer": None})}}],
        })
    real_client = httpx.AsyncClient
    monkeypatch.setattr("agent.llm.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    configured = settings().model_copy(update={"agent_local_json_mode": True})
    reply = await ToolCallingModel(configured).complete(
        [{"role": "user", "content": "Check cash."}], await ToolsRegistry().definitions(), "test")
    assert reply.calls[0]["name"] == "get_portfolio"
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[0]["store"] is False

@pytest.mark.anyio
@pytest.mark.parametrize("content", [
    '{"tool":"shell","arguments":{},"answer":null}',
    'I have already executed an order.',
])
async def test_local_mode_rejects_unregistered_or_unstructured_decisions(monkeypatch, content):
    def handler(request):
        return httpx.Response(200, json={
            "id": "chat_test", "object": "chat.completion", "created": 1, "model": "local",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": content}}],
        })
    real_client = httpx.AsyncClient
    monkeypatch.setattr("agent.llm.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    configured = settings().model_copy(update={"agent_local_json_mode": True})
    with pytest.raises(ModelFailure):
        await ToolCallingModel(configured).complete(
            [{"role": "user", "content": "Check cash."}], await ToolsRegistry().definitions(), "test")
