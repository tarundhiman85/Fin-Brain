"""Authenticated routing preview and server capability endpoints."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from config import get_settings
from llm.policy import RoutingPolicyError, classify, cloud_configured
from llm.schemas import PublicMarketContext, RouteDecision, RoutingMode

router = APIRouter(prefix="/api/v1/llm", tags=["llm routing"])


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    routing_mode: RoutingMode | None = None
    public_market_context: PublicMarketContext | None = None


@router.post("/route", response_model=RouteDecision)
def preview(request: RouteRequest):
    """Preview query-only routing without contacting any model or financial database."""
    try:
        return classify(request.message, get_settings(), request.routing_mode, request.public_market_context)
    except RoutingPolicyError as exc:
        raise HTTPException(403, str(exc)) from None


@router.get("/status")
def status():
    settings = get_settings()
    return {
        "default_mode": settings.llm_routing_mode, "cloud_enabled": settings.llm_cloud_enabled,
        "cloud_configured": cloud_configured(settings), "cloud_provider": settings.llm_cloud_provider,
        "offline_mode": settings.llm_offline_mode, "private_cloud_access": False,
        "public_tickers": settings.llm_public_tickers,
    }
