"""Verified local Ollama generation and embeddings, with a local backup model."""
import anyio
from httpx import AsyncClient
from pydantic import SecretStr

from agent.llm import ModelConfigurationError, ModelFailure, ToolCallingModel
from embeddings import OllamaEmbedder
from llm.local_models import validate_local_model


class OllamaUnavailable(ModelFailure):
    pass


class OllamaClient:
    def __init__(self, settings):
        self.settings = settings

    async def verify_model(self, name):
        try:
            validate_local_model(name, {})
            async with AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = await client.post(f"{self.settings.ollama_url}/api/show", json={"model": name})
                response.raise_for_status()
                metadata = response.json()
            validate_local_model(name, metadata)
        except ValueError as exc:
            raise ModelConfigurationError(str(exc)) from None
        except Exception:
            raise OllamaUnavailable("Local Ollama is unavailable or the selected model is missing.") from None

    async def complete(self, messages, tools, instructions):
        models = [self.settings.llm_local_model or self.settings.agent_model]
        if self.settings.llm_local_fallback_model:
            models.append(self.settings.llm_local_fallback_model)
        for name in dict.fromkeys(models):
            try:
                await self.verify_model(name)
                configured = self.settings.model_copy(update={
                    "agent_model": name,
                    "agent_llm_base_url": self.settings.ollama_url + "/v1",
                    "agent_llm_api_key": SecretStr("local-only"),
                    "agent_allow_remote_llm": False,
                })
                reply = await ToolCallingModel(configured).complete(messages, tools, instructions)
                reply.model_name = name
                return reply
            except ModelConfigurationError as exc:
                raise ModelFailure(str(exc)) from None
            except ModelFailure:
                continue
        raise OllamaUnavailable(
            "Local Ollama inference failed. Private data has not been sent to a cloud provider.")

    async def generate(self, prompt: str) -> str:
        reply = await self.complete(
            [{"role": "user", "content": prompt}], [],
            "Answer the user's question using the supplied context.")
        return reply.text

    async def embed(self, texts: list[str]) -> list[list[float]]:
        await self.verify_model(self.settings.embedding_model)
        return await anyio.to_thread.run_sync(lambda: OllamaEmbedder(self.settings).embed(texts))
