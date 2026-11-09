"""Stdio tools for Yahoo market data and the PostgreSQL virtual trading ledger."""
from pathlib import Path
import sys
from typing import Annotated, Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from mcp_servers.common import lifespan, run_blocking
from trading_service import execute_trade, fetch_stock_quote

mcp = MCPServer("finbrain-market-trading", lifespan=lifespan, log_level="WARNING")


@mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                      open_world_hint=True))
async def get_stock_quote(ticker: str) -> dict[str, Any]:
    """Fetch Yahoo's latest available regular-session one-minute stock bar.

    Returns price, currency, provider timestamp, age and session metrics.
    Quotes can be delayed or stale. The ticker is sent to Yahoo Finance.
    """
    return await run_blocking(lambda: fetch_stock_quote(ticker).model_dump(mode="json"))


@mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                      idempotent_hint=False, open_world_hint=True))
async def execute_paper_trade(
    ticker: str,
    shares: Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)],
    order_type: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Fill a virtual BUY or SELL using the latest available Yahoo stock price.

    Updates virtual cash, average-cost holdings and a CLOSED order atomically.
    Supply a UUID request_id and reuse it with identical arguments for safe retries.
    Omitting request_id creates a new order on every call. Execution requires a
    fresh quote in the portfolio currency and sufficient cash or owned shares.
    """
    return await run_blocking(lambda: execute_trade(ticker, shares, order_type, request_id))


if __name__ == "__main__":
    mcp.run(transport="stdio")
