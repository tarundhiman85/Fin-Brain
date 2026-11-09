"""Discover Phase 2 MCP schemas and execute a narrow, validated agent tool set."""
import json
from typing import Literal
from uuid import UUID, uuid5

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent.portfolio import get_portfolio_metrics
from mcp_servers.common import run_blocking
from mcp_servers.finance_kb_mcp import mcp as finance_mcp
from mcp_servers.market_trading_mcp import mcp as market_mcp
from trading_service import execute_trade, normalize_ticker


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TradeArguments(EmptyArguments):
    ticker: str = Field(min_length=1, max_length=20)
    shares: float = Field(gt=0, allow_inf_nan=False, strict=True)
    order_type: Literal["BUY", "SELL"]
    max_buy_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    min_sell_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_notional: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("shares", "max_buy_price", "min_sell_price", "max_notional", mode="before")
    @classmethod
    def local_model_numbers(cls, value, info):
        if isinstance(value, str):
            if info.field_name != "shares" and value.strip().lower() == "null":
                return None
            return float(value)
        if isinstance(value, bool):
            raise ValueError("Numeric arguments must not be booleans")
        return value


def strict_schema(schema: dict) -> dict:
    """Responses strict tools require every property, with null for optional values."""
    schema = json.loads(json.dumps(schema))
    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema


class ToolsRegistry:
    async def definitions(self) -> list[dict]:
        definitions = []
        for server, names in [
            (finance_mcp, {"search_financial_kb"}), (market_mcp, {"get_stock_quote"}),
        ]:
            for tool in await server.list_tools():
                if tool.name in names:
                    definitions.append({
                        "type": "function", "name": tool.name,
                        "description": tool.description or tool.name, "strict": True,
                        "parameters": strict_schema(tool.input_schema),
                    })
        definitions += [
            {"type": "function", "name": "get_portfolio", "strict": True,
             "description": "Read virtual cash, holdings and indicative realized/unrealized P&L. "
                            "Check current cash/holdings before each paper trading decision.",
             "parameters": strict_schema(EmptyArguments.model_json_schema())},
            {"type": "function", "name": "execute_paper_trade", "strict": True,
             "description": "Execute a user-requested virtual BUY or SELL. Map all stated price "
                            "and budget conditions to max_buy_price, min_sell_price or max_notional. "
                            "Use null for absent limits. Read current portfolio and quote first. "
                            "Identical normalized trade arguments within one chat request replay "
                            "one fill. For an additional intended fill, use a new chat request.",
             "parameters": strict_schema(TradeArguments.model_json_schema())},
        ]
        return definitions

    async def invoke(self, name: str, arguments: dict, turn_id: UUID) -> dict:
        try:
            if name in {"search_financial_kb", "get_stock_quote"}:
                server = finance_mcp if name == "search_financial_kb" else market_mcp
                result = await server.call_tool(name, arguments)
                if result.is_error or result.structured_content is None:
                    raise ToolError("The MCP tool could not complete the request.")
                data = result.structured_content
            elif name == "get_portfolio":
                EmptyArguments.model_validate(arguments)
                data = await self.metrics()
            elif name == "execute_paper_trade":
                args = TradeArguments.model_validate(arguments).model_dump()
                args["ticker"] = normalize_ticker(args["ticker"])
                key = uuid5(turn_id, json.dumps(args, sort_keys=True, separators=(",", ":")))
                data = await run_blocking(lambda: execute_trade(**args, request_id=str(key)))
            else:
                return {"ok": False, "error": "Unknown tool."}
            return {"ok": True, "data": data}
        except ValidationError:
            return {"ok": False, "error": "Tool arguments do not match the input schema."}
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "Tool unavailable. Check local service configuration."}

    async def metrics(self) -> dict:
        return await get_portfolio_metrics()
