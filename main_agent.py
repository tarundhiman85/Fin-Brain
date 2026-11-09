"""Agent router, mounted by main.py under its existing API-key authentication."""
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from agent.llm import ModelConfigurationError, ToolCallingModel
from agent.state import ConversationConflict
from agent.trading_agent import ChatRequest, ChatResponse, TradingAgent
from config import get_settings

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


@lru_cache
def get_agent() -> TradingAgent:
    settings = get_settings()
    try:
        return TradingAgent(ToolCallingModel(settings), settings)
    except ModelConfigurationError as exc:
        raise HTTPException(503, str(exc)) from None


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    agent: Annotated[TradingAgent, Depends(get_agent)],
) -> ChatResponse:
    try:
        return await agent.chat(request)
    except ConversationConflict as exc:
        raise HTTPException(409, str(exc)) from None
