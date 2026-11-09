"""Shared stdio lifecycle and bounded workers for existing synchronous libraries."""
import logging
from contextlib import asynccontextmanager
from typing import Callable, TypeVar

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from config import get_settings
from database import get_engine
from embeddings import EmbeddingError
from finance_service import FinanceSearchError

logger = logging.getLogger(__name__)
Result = TypeVar("Result")
worker_limit = anyio.CapacityLimiter(4)


@asynccontextmanager
async def lifespan(server):
    get_settings()
    try:
        yield {}
    finally:
        await anyio.to_thread.run_sync(get_engine().dispose)


async def run_blocking(operation: Callable[[], Result]) -> Result:
    """Keep SQLAlchemy, Ollama and yfinance work off the protocol event loop."""
    from trading_service import TradingError
    try:
        # Retain the worker until it completes, including its commit/rollback.
        return await anyio.to_thread.run_sync(
            operation, abandon_on_cancel=False, limiter=worker_limit)
    except (TradingError, FinanceSearchError, EmbeddingError) as exc:
        raise ToolError(str(exc)) from None
    except ValidationError:
        raise ToolError("Invalid tool arguments. Check the advertised input schema.") from None
    except SQLAlchemyError:
        raise ToolError(
            "Database operation failed. Check connectivity and run upgrade_phase2.py.") from None
    except Exception as exc:
        # Neither database parameters nor financial contents enter client errors/logs.
        logger.error("Tool operation failed (%s)", type(exc).__name__)
        raise ToolError("Operation failed. Check the local service configuration.") from None
