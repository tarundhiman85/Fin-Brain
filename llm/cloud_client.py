"""Cloud adapters accept only a generated public-market projection."""
import json
from datetime import datetime
from decimal import Decimal
from llm.schemas import PublicMarketContext

from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict, Field

from agent.llm import ModelFailure, ModelReply, ToolCallingModel, reject_nonfinite

PUBLIC_INSTRUCTIONS = """
Analyze the supplied public-market task and named public drivers using the available public quote tool.
The input contains approved symbols and public quote observations only.
Fetch any missing requested quotes, then synthesize the requested comparison or
scenario analysis. Reuse observed quotes instead of repeating identical lookups.
Separate hypothetical reasoning from observed prices. Quote bars alone do not
establish current news or historical trends. Cite the quote timestamps.
Personal accounts and paper trading actions are outside this analysis interface.
""".strip()
TASKS = {
    "overview": "Explain the public market characteristics and latest quotes for these symbols.",
    "comparison": "Compare these publicly traded assets using available quotes and general fundamentals.",
    "risk": "Analyze risk factors and diversification for this hypothetical public asset basket.",
    "scenario": "Discuss hypothetical macroeconomic scenarios for these publicly traded assets.",
    "portfolio_analysis": "Analyze this hypothetical unweighted public asset basket across multiple reasoning steps.",
}


class PublicQuote(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ticker: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    price: Decimal = Field(gt=0, allow_inf_nan=False)
    as_of: datetime
    session_open: Decimal | None = Field(default=None, allow_inf_nan=False)
    session_high: Decimal | None = Field(default=None, allow_inf_nan=False)
    session_low: Decimal | None = Field(default=None, allow_inf_nan=False)
    session_volume: int | None = Field(default=None, ge=0)


def public_projection(decision, messages: list[dict]) -> dict:
    """Discard raw text/history and copy only validated, approved quote fields."""
    context = decision.public_context
    calls, quotes = {}, {}
    for item in messages:
        if item.get("type") == "function_call" and item.get("name") == "get_stock_quote":
            calls[item["call_id"]] = item
        elif item.get("type") == "function_call_output" and item.get("call_id") in calls:
            try:
                result = json.loads(item["output"], parse_constant=reject_nonfinite)
                quote = PublicQuote.model_validate(result["data"])
                if result["ok"] and quote.ticker in context.tickers:
                    quotes[quote.ticker] = quote.model_dump(mode="json")
            except (ValueError, KeyError, TypeError):
                continue
    return {
        "analysis": context.analysis, "task": TASKS[context.analysis],
        "tickers": context.tickers, "horizon": context.horizon, "drivers": context.drivers,
        "observed_quotes": list(quotes.values()),
    }


def sanitize_public_payload(payload, settings):
    """Rebuild the outbound contract even when the adapter is called directly."""
    try:
        context = PublicMarketContext(
            tickers=payload["tickers"], analysis=payload.get("analysis", "overview"),
            horizon=payload.get("horizon", "long_term"), drivers=payload.get("drivers", []))
        if not set(context.tickers).issubset(settings.llm_public_tickers):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ModelFailure("Cloud requests require an approved public-market context.") from None
    quotes = []
    for raw in payload.get("observed_quotes", []):
        try:
            quote = PublicQuote.model_validate(raw)
            if quote.ticker in context.tickers:
                quotes.append(quote.model_dump(mode="json"))
        except (ValueError, TypeError):
            continue
    return {"task": TASKS[context.analysis], "tickers": context.tickers,
            "horizon": context.horizon, "drivers": context.drivers, "observed_quotes": quotes}

def public_quote_tool(tickers):
    """Publish a fixed cloud facade instead of forwarding local MCP descriptions."""
    return {
        "type": "function", "name": "get_stock_quote", "strict": True,
        "description": "Fetch the latest timestamped public quote for an approved symbol.",
        "parameters": {
            "type": "object", "properties": {"ticker": {"type": "string", "enum": tickers}},
            "required": ["ticker"], "additionalProperties": False,
        },
    }

class CloudClient:
    def __init__(self, settings):
        self.settings = settings

    async def complete(self, payload: dict, tools: list[dict]) -> ModelReply:
        settings = self.settings
        safe_payload = sanitize_public_payload(payload, settings)
        tools = [public_quote_tool(safe_payload["tickers"])] if any(
            tool["name"] == "get_stock_quote" for tool in tools) else []
        messages = [{"role": "user", "content": json.dumps(safe_payload)}]
        if settings.llm_cloud_provider == "openai":
            configured = settings.model_copy(update={
                "agent_model": settings.llm_cloud_model,
                "agent_llm_base_url": "https://api.openai.com/v1",
                "agent_llm_api_key": settings.llm_cloud_api_key,
                "agent_allow_remote_llm": True, "agent_local_json_mode": False,
            })
            return await ToolCallingModel(configured).complete(messages, tools, PUBLIC_INSTRUCTIONS)
        try:
            from anthropic import AsyncAnthropic
            async with AsyncAnthropic(
                api_key=settings.llm_cloud_api_key.get_secret_value(),
                base_url="https://api.anthropic.com", max_retries=0,
                http_client=AsyncClient(timeout=settings.agent_model_timeout_seconds,
                                        trust_env=False, follow_redirects=False),
            ) as client:
                response = await client.messages.create(
                    model=settings.llm_cloud_model, max_tokens=settings.agent_max_output_tokens,
                    system=PUBLIC_INSTRUCTIONS, messages=messages,
                    tools=[{"name": tool["name"], "description": tool["description"],
                            "input_schema": tool["parameters"]} for tool in tools],
                )
            if response.stop_reason not in {"tool_use", "end_turn"}:
                raise ModelFailure("The cloud model response was incomplete.")
            items, calls, text = [], [], []
            for block in response.content:
                if block.type == "tool_use":
                    item = {"type": "function_call", "call_id": block.id,
                            "name": block.name, "arguments": json.dumps(block.input, allow_nan=False)}
                    items.append(item)
                    calls.append(item)
                elif block.type == "text":
                    text.append(block.text)
            answer = "\n".join(text)
            if not calls:
                items.append({"role": "assistant", "content": answer})
            return ModelReply(items=items, calls=calls, text=answer)
        except ModelFailure:
            raise
        except Exception:
            raise ModelFailure("The configured cloud model service failed.") from None
