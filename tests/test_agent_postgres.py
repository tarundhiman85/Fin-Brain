"""End-to-end orchestration, persistent recovery and metrics with real PostgreSQL."""
import copy
import json
import os
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import anyio
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from agent.llm import ModelFailure, ModelReply
from agent.models import AgentConversation, AgentTurn
from agent.state import ConversationConflict, StateStore
from agent.tools_registry import ToolsRegistry
from agent.trading_agent import ChatRequest, TradingAgent
from config import get_settings
from models import VirtualOrder, VirtualPortfolio
from tests.test_phase2_mcp import quote
from tests.test_phase2_postgres import ledger
from upgrade_phase3 import upgrade

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"),
                                reason="TEST_DATABASE_URL not provided")


def call(name, arguments, identifier=None):
    item = {"type": "function_call", "name": name, "arguments": json.dumps(arguments),
            "call_id": identifier or "call_" + uuid4().hex, "id": "fc_" + uuid4().hex,
            "status": "completed"}
    return ModelReply(items=[item], calls=[item], text="")


def final(text="Completed the requested checks and paper action."):
    return ModelReply(items=[{"role": "assistant", "content": text}], calls=[], text=text)


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.inputs = []

    async def complete(self, messages, tools, instructions):
        self.inputs.append(copy.deepcopy(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def agent_env(ledger, monkeypatch):
    engine, portfolio_id = ledger
    upgrade(engine)
    upgrade(engine)
    settings = get_settings().model_copy(update={
        "agent_model": "test-model", "paper_portfolio_id": portfolio_id})
    monkeypatch.setattr("agent.portfolio.get_engine", lambda: engine)
    monkeypatch.setattr("agent.portfolio.get_settings", lambda: settings)
    monkeypatch.setattr("agent.portfolio.fetch_stock_quote", lambda ticker: quote(ticker))
    monkeypatch.setattr("mcp_servers.market_trading_mcp.fetch_stock_quote", lambda ticker: quote(ticker))
    conversation = "test-" + uuid4().hex
    try:
        yield engine, settings, conversation
    finally:
        with Session(engine) as session, session.begin():
            session.execute(delete(AgentTurn).where(AgentTurn.conversation_id == conversation))
            session.execute(delete(AgentConversation).where(AgentConversation.id == conversation))


def trade_args(**changes):
    return dict(ticker="AAPL", shares=2, order_type="BUY", max_buy_price=101,
                min_sell_price=None, max_notional=200, **changes)


def test_multi_step_chat_trace_replay_history_and_metrics(agent_env):
    engine, settings, conversation = agent_env
    model = ScriptedModel([
        call("search_financial_kb", {"query": "groceries", "top_k": 5}),
        call("get_portfolio", {}), call("get_stock_quote", {"ticker": "AAPL"}),
        call("execute_paper_trade", trade_args()), final("Bought 2 virtual shares. Grocery evidence was retrieved."),
    ])
    registry = ToolsRegistry()
    # Keep the MCP search boundary while supplying deterministic local evidence.
    registry_search = registry.invoke
    async def invoke(name, args, turn_id):
        if name == "search_financial_kb":
            return {"ok": True, "data": {"results": [{"id": "mock-chunk", "content": "Groceries 12.30"}]}}
        return await registry_search(name, args, turn_id)
    registry.invoke = invoke
    agent = TradingAgent(model, settings, registry=registry, store=StateStore(engine))
    request = ChatRequest(message="Review groceries and buy two Apple shares for at most 200.", conversation_id=conversation)
    response = anyio.run(agent.chat, request)
    assert response.status == "completed" and len(response.steps) == 4
    assert all(step.status == "succeeded" and step.completed_at for step in response.steps)
    assert response.portfolio["cash_balance"] == "800.00"
    assert Decimal(response.portfolio["equity"]) == Decimal("1000")
    replay = anyio.run(agent.chat, request)
    assert replay.replayed and len(model.inputs) == 5
    assert replay.steps[-1].observation["data"]["order_id"] == response.steps[-1].observation["data"]["order_id"]
    following = ScriptedModel([call("get_portfolio", {}), final("I remember the prior paper purchase.")])
    agent.model = following
    anyio.run(agent.chat, ChatRequest(message="What did we just do?", conversation_id=conversation))
    assert any("Bought 2" in message.get("content", "") for message in following.inputs[0])
    assert any(item.get("type") == "function_call_output" for item in model.inputs[-1])


def test_duplicate_model_trade_calls_fill_once(agent_env):
    engine, settings, conversation = agent_env
    model = ScriptedModel([call("execute_paper_trade", trade_args()),
                           call("execute_paper_trade", trade_args()), final()])
    response = anyio.run(TradingAgent(model, settings, store=StateStore(engine)).chat,
                        ChatRequest(message="Buy two Apple shares.", conversation_id=conversation))
    assert response.steps[1].observation["data"]["replayed"]
    assert response.portfolio["cash_balance"] == "800.00"


def test_lost_checkpoint_after_commit_resumes_without_double_fill(agent_env, monkeypatch):
    engine, settings, conversation = agent_env
    store = StateStore(engine)
    save = store.save
    failed = False
    def fail_after_fill(identifier, state):
        nonlocal failed
        if state["steps"] and state["steps"][-1]["status"] == "succeeded" and not failed:
            failed = True
            raise OperationalError("checkpoint failed", {}, Exception("test disconnect"))
        save(identifier, state)
    monkeypatch.setattr(store, "save", fail_after_fill)
    model = ScriptedModel([call("execute_paper_trade", trade_args()), final()])
    agent = TradingAgent(model, settings, store=store)
    request = ChatRequest(message="Buy two Apple shares.", conversation_id=conversation)
    with pytest.raises(OperationalError):
        anyio.run(agent.chat, request)
    # A new process can resume the same durable pending tool call.
    resumed = TradingAgent(model, settings, store=StateStore(engine))
    response = anyio.run(resumed.chat, request)
    assert response.steps[0].observation["data"]["replayed"]
    assert response.portfolio["cash_balance"] == "800.00"
    with Session(engine) as session:
        orders = session.scalars(select(VirtualOrder).where(
            VirtualOrder.portfolio_id == settings.paper_portfolio_id)).all()
        assert len(orders) == 1


def test_model_failure_after_fill_returns_trace_and_metrics(agent_env):
    engine, settings, conversation = agent_env
    model = ScriptedModel([call("execute_paper_trade", trade_args()), ModelFailure("Model unavailable.")])
    response = anyio.run(TradingAgent(model, settings, store=StateStore(engine)).chat,
                        ChatRequest(message="Buy Apple.", conversation_id=conversation))
    assert response.status == "failed" and response.steps[0].status == "succeeded"
    assert response.portfolio["cash_balance"] == "800.00"


def test_request_id_conflict_and_cross_process_conversation_lock(agent_env):
    engine, settings, conversation = agent_env
    store = StateStore(engine)
    other = StateStore(engine)
    async def check():
        async with store.lock(conversation):
            with pytest.raises(ConversationConflict):
                async with other.lock(conversation):
                    pass
    anyio.run(check)
    request = ChatRequest(message="Check cash.", conversation_id=conversation)
    agent = TradingAgent(ScriptedModel([call("get_portfolio", {}), final()]), settings, store=store)
    anyio.run(agent.chat, request)
    changed = request.model_copy(update={"message": "Different message."})
    with pytest.raises(ConversationConflict):
        anyio.run(agent.chat, changed)


def test_loop_limit_and_tool_validation_are_observable(agent_env):
    engine, settings, conversation = agent_env
    settings = settings.model_copy(update={"agent_max_steps": 2})
    model = ScriptedModel([call("unknown_tool", {}), call("execute_paper_trade", {"shares": -1})])
    response = anyio.run(TradingAgent(model, settings, store=StateStore(engine)).chat,
                        ChatRequest(message="Check something.", conversation_id=conversation))
    assert response.status == "limit_reached"
    assert all(step.status == "error" for step in response.steps)
    assert response.portfolio["cash_balance"] == "1000.00"


def test_api_auth_and_persistent_chat_response(agent_env):
    from fastapi.testclient import TestClient
    from main import app
    from main_agent import get_agent
    engine, settings, conversation = agent_env
    agent = TradingAgent(ScriptedModel([call("get_portfolio", {}), final("Your paper balance is available.")]),
                         settings, store=StateStore(engine))
    app.dependency_overrides[get_agent] = lambda: agent
    try:
        with TestClient(app) as client:
            body = {"message": "Check cash.", "conversation_id": conversation,
                    "request_id": str(uuid4())}
            assert client.post("/api/v1/agent/chat", json=body).status_code == 401
            headers = {"X-API-Key": get_settings().api_key.get_secret_value()}
            result = client.post("/api/v1/agent/chat", json=body, headers=headers)
            assert result.status_code == 200 and result.json()["status"] == "completed"
            repeated = client.post("/api/v1/agent/chat", json=body, headers=headers)
            assert repeated.json()["replayed"]
    finally:
        app.dependency_overrides.clear()


def test_realized_unrealized_pnl_and_missing_quote(agent_env, monkeypatch):
    from agent.portfolio import get_portfolio_metrics
    from trading_service import execute_trade
    engine, settings, conversation = agent_env
    execute_trade("AAPL", 2, "BUY")
    monkeypatch.setattr("trading_service.fetch_stock_quote", lambda ticker: quote(ticker, "120"))
    execute_trade("AAPL", 1, "SELL")
    monkeypatch.setattr("agent.portfolio.fetch_stock_quote", lambda ticker: quote(ticker, "130"))
    metrics = anyio.run(get_portfolio_metrics)
    assert Decimal(metrics["realized_pnl"]) == Decimal("20")
    assert Decimal(metrics["unrealized_pnl"]) == Decimal("30")
    assert Decimal(metrics["total_pnl"]) == Decimal("50")
    assert Decimal(metrics["equity"]) == Decimal("1050")
    monkeypatch.setattr("agent.portfolio.fetch_stock_quote", Mock(side_effect=RuntimeError("offline")))
    metrics = anyio.run(get_portfolio_metrics)
    assert metrics["equity"] is None and not metrics["valuation_complete"]
    assert metrics["cash_balance"] == "920.00"

def test_failed_trade_cannot_be_reported_as_a_model_claimed_fill(agent_env):
    engine, settings, conversation = agent_env
    args = trade_args()
    args["max_notional"] = 100
    model = ScriptedModel([
        call("execute_paper_trade", args),
        final("Bought two shares successfully. This claim must be replaced."),
    ])
    response = anyio.run(TradingAgent(model, settings, store=StateStore(engine)).chat,
                        ChatRequest(message="Buy within my budget.", conversation_id=conversation))
    assert response.steps[0].status == "error"
    assert "Paper order rejected" in response.answer
    assert "Bought two shares successfully" not in response.answer
    assert response.portfolio["cash_balance"] == "1000.00"

def test_prose_tool_plan_requests_native_protocol_correction(agent_env):
    engine, settings, conversation = agent_env
    plan = final('{"name": "get_portfolio", "parameters": {}}')
    model = ScriptedModel([plan, call("get_portfolio", {}), final("Balance checked.")])
    response = anyio.run(TradingAgent(model, settings, store=StateStore(engine)).chat,
                        ChatRequest(message="Check cash.", conversation_id=conversation))
    assert response.status == "completed" and len(response.steps) == 1
    assert any(message.get("role") == "developer" for message in model.inputs[1])

def test_model_cannot_invent_observations_without_native_calls(agent_env):
    engine, settings, conversation = agent_env
    model = ScriptedModel([final("Bought fake shares."), final("Bought fake shares again.")])
    response = anyio.run(TradingAgent(model, settings, store=StateStore(engine)).chat,
                        ChatRequest(message="Check cash and buy Apple.", conversation_id=conversation))
    assert response.status == "failed" and response.steps == []
    assert "no tool actions" in response.answer
    assert "Bought fake" not in response.answer
    assert response.portfolio["cash_balance"] == "1000.00"

def test_nonfinite_native_tool_arguments_become_recorded_rejections(agent_env):
    engine, settings, conversation = agent_env
    request = call("execute_paper_trade", {})
    request.calls[0]["arguments"] = '{"ticker":"AAPL","shares":NaN,"order_type":"BUY"}'
    model = ScriptedModel([request, final("The attempted order failed.")])
    response = anyio.run(TradingAgent(model, settings, store=StateStore(engine)).chat,
                        ChatRequest(message="Buy Apple.", conversation_id=conversation))
    assert response.steps[0].status == "error"
    assert "NaN" in response.steps[0].raw_arguments
    assert response.portfolio["cash_balance"] == "1000.00"

@pytest.mark.parametrize("field,value", [("name", None), ("arguments", {})])
def test_invalid_provider_call_metadata_finishes_without_pending_corruption(agent_env, field, value):
    engine, settings, conversation = agent_env
    malformed = call("get_portfolio", {})
    malformed.calls[0][field] = value
    model = ScriptedModel([malformed])
    response = anyio.run(TradingAgent(model, settings, store=StateStore(engine)).chat,
                        ChatRequest(message="Check cash.", conversation_id=conversation))
    assert response.status == "failed" and response.steps == []
    assert response.portfolio["cash_balance"] == "1000.00"
