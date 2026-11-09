"""Authenticated single-owner financial retrieval and paper portfolio API."""
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import get_settings
from database import get_engine, get_session
from embeddings import EmbeddingError, OllamaEmbedder, get_embedder
from models import VirtualPortfolio, VirtualPosition
from finance_service import (
    FinanceQuery, FinanceSearchError, SearchHit, SearchResponse, search_financial_documents,
)

key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def authenticate(key: Annotated[str | None, Security(key_header)]):
    expected = get_settings().api_key.get_secret_value()
    if key is None or not secrets.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings()  # Fail startup immediately if required secrets are missing.
    yield
    get_engine().dispose()


app = FastAPI(title="Private Finance Auditor", version="0.1.0", lifespan=lifespan,
              dependencies=[Depends(authenticate)])


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Database unavailable or not initialized"})


@app.exception_handler(EmbeddingError)
async def embedding_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Local embedding service unavailable"})


@app.post("/api/v1/finance/query", response_model=SearchResponse)
def finance_query(
    body: FinanceQuery,
    session: Annotated[Session, Depends(get_session)],
    embedder: Annotated[OllamaEmbedder, Depends(get_embedder)],
):
    try:
        return search_financial_documents(session, embedder, body)
    except FinanceSearchError as exc:
        raise HTTPException(503, str(exc)) from None


class Holding(BaseModel):
    ticker: str
    shares: Decimal
    buy_price: Decimal
    cost_basis: Decimal
    timestamp: datetime


class PortfolioResponse(BaseModel):
    id: int
    currency: str
    cash_balance: Decimal
    holdings_cost_basis: Decimal
    updated_at: datetime
    holdings: list[Holding]


@app.get("/api/v1/trading/portfolio", response_model=PortfolioResponse)
def trading_portfolio(
    session: Annotated[Session, Depends(get_session)],
    portfolio_id: Annotated[int, Query(ge=1)] = 1,
):
    # Read the cash and positions from one PostgreSQL MVCC snapshot.
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    portfolio = session.get(VirtualPortfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(404, "Virtual portfolio not found; run database.py")
    positions = session.scalars(
        select(VirtualPosition).where(
            VirtualPosition.portfolio_id == portfolio_id, VirtualPosition.status == "OPEN"
        ).order_by(VirtualPosition.ticker)
    ).all()
    holdings = [
        Holding(ticker=p.ticker, shares=p.shares, buy_price=p.buy_price,
                cost_basis=p.shares * p.buy_price, timestamp=p.timestamp)
        for p in positions
    ]
    return PortfolioResponse(
        id=portfolio.id, currency=portfolio.currency, cash_balance=portfolio.cash_balance,
        holdings_cost_basis=sum((p.cost_basis for p in holdings), Decimal("0")),
        updated_at=portfolio.updated_at, holdings=holdings,
    )
