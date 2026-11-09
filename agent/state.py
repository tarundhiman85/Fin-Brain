"""Short checkpoint transactions and a cross-process lock per conversation."""
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import UUID

import anyio
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from agent.models import AgentConversation, AgentTurn
from database import get_engine


class ConversationConflict(ValueError):
    pass


class StateStore:
    def __init__(self, engine=None):
        self.engine = engine if engine is not None else get_engine()

    @asynccontextmanager
    async def lock(self, conversation_id: str):
        # The dedicated connection touches only an advisory lock. Checkpoints and
        # trades commit on separate connections, so an interrupted turn is resumable.
        with anyio.CancelScope(shield=True):
            connection = await anyio.to_thread.run_sync(self.engine.connect)
        try:
            def acquire():
                connection.execute(text("SET LOCAL idle_in_transaction_session_timeout = '0'"))
                return connection.scalar(text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(:id, 9031))"),
                    {"id": conversation_id})
            acquired = await anyio.to_thread.run_sync(acquire)
            if not acquired:
                raise ConversationConflict("This conversation is already processing a request.")
            yield
        finally:
            with anyio.CancelScope(shield=True):
                # close rolls back the lock transaction before returning to the pool.
                await anyio.to_thread.run_sync(connection.close)

    def start(self, conversation_id: str, request_id: UUID, message: str, max_history_chars=50000) -> dict:
        with Session(self.engine) as session, session.begin():
            session.execute(insert(AgentConversation).values(id=conversation_id, history=[])
                            .on_conflict_do_nothing(index_elements=["id"]))
            conversation = session.get(AgentConversation, conversation_id)
            turn = session.get(AgentTurn, request_id)
            if turn is not None:
                if turn.conversation_id != conversation_id or turn.message != message:
                    raise ConversationConflict("request_id was used for different chat arguments.")
                return {"state": deepcopy(turn.state), "response": deepcopy(turn.response)}
            if conversation.active_request_id is not None:
                raise ConversationConflict(
                    f"Resume the unfinished request {conversation.active_request_id} first.")
            history = deepcopy(conversation.history)
            while history and len(json.dumps(history)) + len(message) > max_history_chars:
                history = history[2:]
            state = {
                "history": history,
                "input": deepcopy(history) + [{"role": "user", "content": message}],
                "steps": [], "pending": [], "rounds": 0,
            }
            session.add(AgentTurn(request_id=request_id, conversation_id=conversation_id,
                                  message=message, state=state))
            conversation.active_request_id = request_id
            return {"state": state, "response": None}

    def save(self, request_id: UUID, state: dict):
        with Session(self.engine) as session, session.begin():
            session.get(AgentTurn, request_id).state = deepcopy(state)

    def finish(self, request_id: UUID, state: dict, response: dict):
        with Session(self.engine) as session, session.begin():
            turn = session.get(AgentTurn, request_id)
            conversation = session.get(AgentConversation, turn.conversation_id)
            turn.state = deepcopy(state)
            turn.response = deepcopy(response)
            history = state["history"] + [
                {"role": "user", "content": turn.message},
                {"role": "assistant", "content": response["answer"]},
            ]
            # Retain whole user/assistant pairs. Full traces remain in agent_turns.
            conversation.history = history[-40:]
            conversation.active_request_id = None
