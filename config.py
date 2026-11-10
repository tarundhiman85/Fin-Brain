"""Local-only configuration. Secrets are never logged."""
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal
import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parent / ".env", extra="ignore")
    database_url: SecretStr
    api_key: SecretStr = Field(min_length=32)
    ui_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:3000", "http://127.0.0.1:3000"])
    ollama_url: str = "http://127.0.0.1:11434"
    embedding_model: str = "nomic-embed-text:latest"
    embedding_dimensions: int = 768
    agent_llm_base_url: str = "http://127.0.0.1:11434/v1"
    agent_llm_api_key: SecretStr = SecretStr("local-only")
    agent_allow_remote_llm: bool = False
    agent_local_json_mode: bool = True
    agent_model: str = ""
    agent_model_timeout_seconds: int = Field(default=60, ge=1, le=300)
    agent_max_steps: int = Field(default=8, ge=1, le=30)
    agent_max_tool_calls: int = Field(default=16, ge=1, le=60)
    agent_max_output_tokens: int = Field(default=2000, ge=256, le=16000)
    agent_max_context_chars: int = Field(default=100000, ge=10000, le=500000)
    llm_routing_mode: Literal["auto", "local_only", "cloud_only"] = "auto"
    llm_local_model: str = ""
    llm_local_fallback_model: str = ""
    llm_cloud_enabled: bool = False
    llm_cloud_provider: Literal["openai", "anthropic"] = "openai"
    llm_cloud_model: str = ""
    llm_cloud_api_key: SecretStr = SecretStr("")
    llm_complexity_threshold: int = Field(default=2, ge=1, le=20)
    llm_cloud_fallback_local: bool = True
    llm_public_cloud_fallback: bool = False
    llm_offline_mode: bool = False
    llm_public_tickers: list[str] = Field(default_factory=lambda: [
        "AAPL", "MSFT", "TSLA", "NVDA", "AMZN", "GOOGL", "GOOG", "META",
        "JPM", "BRK-B", "SPY", "QQQ", "VTI", "VOO", "DIA", "IWM", "TLT", "GLD"])
    paper_portfolio_id: int = Field(default=1, ge=1)
    max_trade_quote_age_seconds: int = Field(default=1200, ge=1, le=86400)

    @field_validator("ui_origins")
    @classmethod
    def explicit_ui_origins(cls, values):
        for value in values:
            url = urlparse(value)
            if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                    or url.password or url.query or url.fragment or url.path
                    or "*" in value):
                raise ValueError("UI origins must be explicit HTTP(S) origins without paths")
            _ = url.port
        return list(dict.fromkeys(values))

    @field_validator("llm_public_tickers")
    @classmethod
    def approved_tickers(cls, values):
        values = list(dict.fromkeys(value.strip().upper() for value in values))
        if not values or any(not re.fullmatch(r"[A-Z][A-Z0-9.^=-]{0,19}", value) for value in values):
            raise ValueError("Configure explicit public ticker symbols")
        return values

    @field_validator("ollama_url")
    @classmethod
    def local_ollama(cls, value: str) -> str:
        url = urlparse(value)
        if (url.scheme != "http" or url.hostname not in
                {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
                or url.username or url.password or url.query or url.fragment
                or url.path not in {"", "/"}):
            raise ValueError("OLLAMA_URL must point to a local Ollama HTTP service")
        return value.rstrip("/")

    @field_validator("embedding_model")
    @classmethod
    def supported_model(cls, value: str) -> str:
        if value.split(":")[0] != "nomic-embed-text" or "cloud" in value.lower():
            raise ValueError("Phase 1 supports local nomic-embed-text only")
        return value

    @field_validator("embedding_dimensions")
    @classmethod
    def supported_dimensions(cls, value: int) -> int:
        if value != 768:
            raise ValueError("nomic-embed-text schema requires 768 dimensions")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
