"""Database-backed browser order history boundaries."""
import os
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from models import VirtualOrder
from tests.test_phase2_postgres import ledger
import main_ui_bridge as bridge

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not provided")


def test_order_keyset_pagination_preserves_exact_values_and_scope(ledger, monkeypatch):
    engine, portfolio_id = ledger
    monkeypatch.setattr(bridge, "get_engine", lambda: engine)
    monkeypatch.setattr(bridge, "get_settings", lambda: SimpleNamespace(paper_portfolio_id=portfolio_id))
    instant = datetime(2025, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
    with Session(engine) as session, session.begin():
        for index in range(3):
            session.add(VirtualOrder(portfolio_id=portfolio_id, ticker="AAPL", shares=Decimal("1.00000001"),
                                     buy_price=Decimal("123.45678901"), order_type="BUY", status="CLOSED",
                                     timestamp=instant, execution_metadata={"settled_notional": "123.46", "private": "SECRET"}))
    first = bridge.order_history(limit=2)
    second = bridge.order_history(limit=2, before_id=first["next_before_id"])
    assert len(first["orders"]) == 2 and len(second["orders"]) == 1
    assert first["currency"] == "USD" and second["next_before_id"] is None
    assert first["orders"][0]["shares"] == "1.00000001"
    assert first["orders"][0]["fill_price"] == "123.45678901"
    assert first["orders"][0]["timestamp"] == instant.isoformat()
    assert first["orders"][0]["settled_notional"] == "123.46"
    assert "SECRET" not in str(first)
    ids = [row["id"] for page in (first, second) for row in page["orders"]]
    assert len(set(ids)) == 3 and ids == sorted(ids, reverse=True)


def test_request_progress_uses_durable_state_and_saved_response(ledger, monkeypatch):
    from uuid import uuid4
    from sqlalchemy import delete
    from agent.models import AgentConversation, AgentTurn
    engine, _ = ledger
    request_id, conversation_id = uuid4(), f"ui-test-{uuid4()}"
    monkeypatch.setattr(bridge, "get_engine", lambda: engine)
    with Session(engine) as session, session.begin():
        session.add(AgentConversation(id=conversation_id, history=[]))
        session.flush()
        session.add(AgentTurn(request_id=request_id, conversation_id=conversation_id,
                              message="private query", state={"input": ["SECRET_HISTORY"],
                              "steps": [{"tool": "get_portfolio", "status": "pending"}]}))
    try:
        pending = bridge.read_turn_progress(request_id, conversation_id)
        assert pending["steps"][0]["status"] == "pending" and pending["response"] is None
        assert "SECRET_HISTORY" not in str(pending)
        with pytest.raises(ValueError):
            bridge.read_turn_progress(request_id, "wrong-conversation")
        with Session(engine) as session, session.begin():
            session.get(AgentTurn, request_id).response = {"answer": "Saved answer"}
        assert bridge.read_turn_progress(request_id, conversation_id)["response"]["answer"] == "Saved answer"
    finally:
        with Session(engine) as session, session.begin():
            session.execute(delete(AgentTurn).where(AgentTurn.request_id == request_id))
            session.execute(delete(AgentConversation).where(AgentConversation.id == conversation_id))
