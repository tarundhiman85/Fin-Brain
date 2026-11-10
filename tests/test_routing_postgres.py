"""Real persistence tests for routing controls, history privacy and safe replay."""
import anyio
import pytest
from sqlalchemy.orm import Session

from agent.models import AgentConversation, AgentTurn
from agent.state import ConversationConflict, StateStore
from agent.trading_agent import ChatRequest, TradingAgent
from llm.policy import RoutingPolicyError
from llm.router import HybridRouter
from tests.test_agent_postgres import agent_env, call, final, ScriptedModel
from tests.test_phase2_postgres import ledger

# Match the existing integration-test opt-in.
import os
pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"),
                                reason="TEST_DATABASE_URL not provided")


def test_private_cloud_rejection_rolls_back_active_request(agent_env):
    engine, settings, conversation = agent_env
    settings = settings.model_copy(update={"llm_cloud_enabled": True})
    request = ChatRequest(message="Audit my bank statement.", conversation_id=conversation,
                          routing_mode="cloud_only")
    agent = TradingAgent(HybridRouter(settings, local=ScriptedModel([]), cloud=ScriptedModel([])),
                         settings, store=StateStore(engine))
    with pytest.raises(RoutingPolicyError):
        anyio.run(agent.chat, request)
    with Session(engine) as session:
        assert session.get(AgentTurn, request.request_id) is None
        assert session.get(AgentConversation, conversation) is None


def test_legacy_history_is_private_and_routing_options_are_idempotent(agent_env):
    engine, settings, conversation = agent_env
    store = StateStore(engine)
    legacy = TradingAgent(ScriptedModel([call("get_portfolio", {}), final("Private balance reviewed.")]),
                          settings, store=store)
    anyio.run(legacy.chat, ChatRequest(message="Check my cash.", conversation_id=conversation))
    local = ScriptedModel([call("get_portfolio", {}), final("Still local.")])
    router = HybridRouter(settings, local=local, cloud=ScriptedModel([]))
    agent = TradingAgent(router, settings, store=store)
    request = ChatRequest(message="Compare AAPL and MSFT market risk.", conversation_id=conversation)
    response = anyio.run(agent.chat, request)
    assert response.routing["conversation_private"] and response.routing["engine"] == "local"
    assert response.routing_events[0]["provider"] == "ollama"
    assert anyio.run(agent.chat, request).replayed
    with pytest.raises(ConversationConflict):
        anyio.run(agent.chat, request.model_copy(update={"routing_mode": "cloud_only"}))


def test_new_private_route_retains_ledger_trade_semantics(agent_env, monkeypatch):
    engine, settings, conversation = agent_env
    import trading_service
    monkeypatch.setattr(trading_service, "get_settings", lambda: settings)
    local = ScriptedModel([
        call("get_portfolio", {}), call("get_stock_quote", {"ticker": "AAPL"}),
        call("execute_paper_trade", {
            "ticker": "AAPL", "shares": 1, "order_type": "BUY",
            "max_buy_price": None, "min_sell_price": None, "max_notional": 100}),
        final("Filled."),
    ])
    agent = TradingAgent(HybridRouter(settings, local=local, cloud=ScriptedModel([])),
                         settings, store=StateStore(engine))
    request = ChatRequest(message="Check my virtual cash and buy 1 AAPL paper share.",
                          conversation_id=conversation, routing_mode="local_only")
    response = anyio.run(agent.chat, request)
    assert response.status == "completed" and response.portfolio["cash_balance"] == "900.00"
    assert response.routing["engine"] == "local"
    assert anyio.run(agent.chat, request).replayed


def test_offline_recovery_can_read_committed_receipt_without_new_market_access(agent_env, monkeypatch):
    from agent.tools_registry import ToolsRegistry
    from llm.router import RoutingSession
    from llm.schemas import RouteDecision
    from llm.tools import RoutedToolsRegistry
    from uuid import uuid4
    import trading_service

    engine, settings, conversation = agent_env
    turn_id = uuid4()
    args = {"ticker": "AAPL", "shares": 1, "order_type": "BUY"}
    first = anyio.run(ToolsRegistry().invoke, "execute_paper_trade", args, turn_id)
    assert first["ok"]
    offline = settings.model_copy(update={"llm_offline_mode": True})
    monkeypatch.setattr(trading_service, "get_settings", lambda: offline)
    state = {"routing": RouteDecision(
        mode="local_only", engine="local", provider="ollama", model="test",
        sensitive=True, conversation_private=True, complexity_score=0,
        reason="offline", market_access=False).model_dump(), "routing_events": []}
    session = RoutingSession(HybridRouter(offline), state)
    registry = RoutedToolsRegistry(ToolsRegistry(), session)
    replay = anyio.run(registry.invoke, "execute_paper_trade", args, turn_id)
    assert replay["ok"] and replay["data"]["replayed"]
    fresh = anyio.run(registry.invoke, "execute_paper_trade", args, uuid4())
    assert not fresh["ok"]

def test_privacy_taint_survives_a_public_turn_and_timestamp_reordering(agent_env):
    from datetime import datetime, timedelta, timezone
    from pydantic import SecretStr
    from agent.llm import ModelReply

    engine, settings, conversation = agent_env
    settings = settings.model_copy(update={
        "llm_cloud_enabled": True, "llm_cloud_model": "test-cloud",
        "llm_cloud_api_key": SecretStr("fake-key")})
    class Cloud:
        def __init__(self):
            self.calls = []
            self.model = ScriptedModel([
                call("get_stock_quote", {"ticker": "AAPL"}), final("Public market analysis.")])
        async def complete(self, payload, tools):
            self.calls.append(payload)
            return await self.model.complete([], tools, "")
    cloud = Cloud()
    local = ScriptedModel([call("get_portfolio", {}), final("Private review.")])
    agent = TradingAgent(HybridRouter(settings, local=local, cloud=cloud),
                         settings, store=StateStore(engine))
    public = ChatRequest(message="Compare AAPL and MSFT market risk.", conversation_id=conversation)
    first = anyio.run(agent.chat, public)
    assert not first.routing["conversation_private"]
    private = ChatRequest(message="Check my virtual cash.", conversation_id=conversation)
    assert anyio.run(agent.chat, private).routing["conversation_private"]
    with Session(engine) as session, session.begin():
        session.get(AgentTurn, private.request_id).created_at = datetime.now(timezone.utc) - timedelta(days=1)
    with pytest.raises(RoutingPolicyError):
        anyio.run(agent.chat, ChatRequest(
            message="Compare AAPL and MSFT risk.", conversation_id=conversation,
            routing_mode="cloud_only"))
    assert len(cloud.calls) == 2
