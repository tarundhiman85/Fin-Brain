"""Public routing controls and explicit public-market context."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RoutingMode = Literal["auto", "local_only", "cloud_only"]


class PublicMarketContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tickers: list[str] = Field(min_length=1, max_length=6)
    analysis: Literal["overview", "comparison", "risk", "scenario", "portfolio_analysis"] = "overview"
    horizon: Literal["short_term", "long_term"] = "long_term"
    drivers: list[Literal[
        "interest_rates", "inflation", "earnings", "valuation", "diversification",
        "recession", "ai_demand", "trade_policy", "regulation", "liquidity",
    ]] = Field(default_factory=list, max_length=10)

    @field_validator("tickers")
    @classmethod
    def normalize(cls, values):
        return list(dict.fromkeys(value.strip().upper() for value in values))


class RouteDecision(BaseModel):
    mode: RoutingMode
    engine: Literal["local", "cloud"]
    provider: Literal["ollama", "openai", "anthropic"]
    model: str
    sensitive: bool
    conversation_private: bool
    complexity_score: int
    reason: str
    market_access: bool
    public_context: PublicMarketContext | None = None
