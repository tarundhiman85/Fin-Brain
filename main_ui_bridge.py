"""Local browser CORS, read-only dashboard data and durable checkpoint SSE."""
import json
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

import anyio
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.models import AgentTurn
from agent.portfolio import get_portfolio_metrics
from config import get_settings
from database import get_engine
from models import VirtualOrder, VirtualPortfolio
from market_history import CandleHistory, fetch_candle_history
from trading_service import TradingError, fetch_stock_quote, normalize_ticker

router = APIRouter(prefix="/api/v1", tags=["browser interface"])
quote_limiter = anyio.CapacityLimiter(4)


def configure_ui(app: FastAPI):
    app.add_middleware(
        CORSMiddleware, allow_origins=get_settings().ui_origins,
        allow_credentials=False, allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key"], max_age=600,
    )

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


@router.get("/trading/portfolio/metrics")
async def portfolio_metrics():
    settings = get_settings()
    try:
        return await get_portfolio_metrics(
            include_quotes=not settings.llm_offline_mode,
            approved_tickers=set(settings.llm_public_tickers))
    except TradingError as exc:
        raise HTTPException(404, str(exc)) from None


@router.get("/trading/orders")
def order_history(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
):
    """Keyset pagination over the configured portfolio's persisted order ledger."""
    with Session(get_engine()) as session:
        portfolio_id = get_settings().paper_portfolio_id
        portfolio = session.get(VirtualPortfolio, portfolio_id)
        if portfolio is None:
            raise HTTPException(404, "Virtual portfolio not found")
        statement = select(VirtualOrder).where(
            VirtualOrder.portfolio_id == portfolio_id)
        if before_id is not None:
            statement = statement.where(VirtualOrder.id < before_id)
        rows = session.scalars(statement.order_by(VirtualOrder.id.desc()).limit(limit + 1)).all()
        orders = []
        for row in rows[:limit]:
            metadata = row.execution_metadata or {}
            orders.append({
                "id": row.id, "ticker": row.ticker, "shares": str(row.shares),
                "fill_price": str(row.buy_price), "order_type": row.order_type,
                "timestamp": row.timestamp.isoformat(), "status": row.status,
                "request_id": str(row.request_id) if row.request_id else None,
                "settled_notional": metadata.get("settled_notional"),
                "quote_as_of": metadata.get("quote_as_of"),
            })
        return {"currency": portfolio.currency, "orders": orders, "next_before_id": rows[limit - 1].id if len(rows) > limit else None}


@router.get("/market/quote/{ticker}")
async def stock_quote(ticker: str):
    settings = get_settings()
    if settings.llm_offline_mode:
        raise HTTPException(403, "External quotes are disabled by offline mode")
    try:
        ticker = normalize_ticker(ticker)
    except TradingError as exc:
        raise HTTPException(422, str(exc)) from None
    if ticker not in settings.llm_public_tickers:
        raise HTTPException(403, "Ticker is outside the configured public market scope")
    try:
        quote = await anyio.to_thread.run_sync(lambda: fetch_stock_quote(ticker), limiter=quote_limiter)
        return quote.model_dump(mode="json")
    except TradingError as exc:
        raise HTTPException(503, str(exc)) from None


@router.get("/trading/quotes/{ticker}/history", response_model=CandleHistory)
async def candle_history(ticker: str, period: Literal["1y", "2y"] = "2y"):
    settings = get_settings()
    if settings.llm_offline_mode:
        raise HTTPException(403, "Market history is disabled by offline mode")
    try:
        ticker = normalize_ticker(ticker)
    except TradingError as exc:
        raise HTTPException(422, str(exc)) from None
    if ticker not in settings.llm_public_tickers:
        raise HTTPException(403, "Ticker is outside the configured public market scope")
    try:
        return await anyio.to_thread.run_sync(
            lambda: fetch_candle_history(ticker, period), limiter=quote_limiter)
    except TradingError as exc:
        raise HTTPException(503, str(exc)) from None


def read_turn_progress(request_id: UUID, conversation_id: str):
    with Session(get_engine()) as session:
        turn = session.get(AgentTurn, request_id)
        if turn is None:
            return None
        if turn.conversation_id != conversation_id:
            raise ValueError("Request and conversation do not match")
        # Raw prompts/history and internal protocol inputs stay out of the stream.
        return {"steps": turn.state.get("steps", []),
                "routing": turn.state.get("routing"),
                "routing_events": turn.state.get("routing_events", []),
                "response": turn.response}


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, allow_nan=False)}\n\n"


async def progress_events(request: Request, request_id: UUID, conversation_id: str):
    started, last = monotonic(), None
    while monotonic() - started < 900:
        if await request.is_disconnected():
            return
        try:
            progress = await anyio.to_thread.run_sync(read_turn_progress, request_id, conversation_id)
        except Exception:
            yield sse("stream_error", {"detail": "Tool progress is unavailable. The chat request may still be running."})
            return
        if progress is None:
            if monotonic() - started > 15:
                yield sse("stream_error", {"detail": "Request checkpoint not found. Review the chat response or retry its request ID."})
                return
            yield ": waiting for checkpoint\n\n"
        elif progress["response"] is not None:
            yield sse("completed", progress)
            return
        else:
            frame = sse("progress", progress)
            yield frame if frame != last else ": keep-alive\n\n"
            last = frame
        await anyio.sleep(1)
    yield sse("stream_error", {"detail": "Progress stream timed out. Resume the same request to recover its result."})


@router.get("/agent/requests/{request_id}/events")
async def request_events(
    request: Request, request_id: UUID,
    conversation_id: Annotated[str, Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")],
):
    """Observe saved tool checkpoints. This GET never starts or repeats a trade."""
    return StreamingResponse(progress_events(request, request_id, conversation_id),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
