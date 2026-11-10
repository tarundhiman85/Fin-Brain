"""Local Ollama embeddings with explicit nomic retrieval prefixes."""
import math
from functools import lru_cache

import httpx

from config import get_settings
from llm.local_models import validate_local_model


class EmbeddingError(RuntimeError):
    pass


class OllamaEmbedder:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else get_settings()

    @property
    def identity(self) -> str:
        # Records provider, model digest, dimensions and preprocessing contract.
        try:
            with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
                response = client.get(f"{self.settings.ollama_url}/api/tags")
                response.raise_for_status()
                models = response.json()["models"]
            model = next(m for m in models if m["name"] == self.settings.embedding_model)
            return f"ollama:{model['digest']}:768:nomic-prefix-v1"
        except (httpx.HTTPError, ValueError, KeyError, TypeError, StopIteration) as exc:
            raise EmbeddingError("Local embedding model unavailable; pull the configured model") from exc

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        prefix = "search_query: " if query else "search_document: "
        try:
            with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
                validate_local_model(self.settings.embedding_model, {})
                model_info = client.post(
                    f"{self.settings.ollama_url}/api/show", json={"model": self.settings.embedding_model})
                model_info.raise_for_status()
                validate_local_model(self.settings.embedding_model, model_info.json())
                response = client.post(
                    f"{self.settings.ollama_url}/api/embed",
                    json={"model": self.settings.embedding_model,
                          "input": [prefix + text for text in texts], "truncate": False},
                )
                response.raise_for_status()
                vectors = response.json()["embeddings"]
            if len(vectors) != len(texts):
                raise ValueError("Embedding count mismatch")
            for vector in vectors:
                if (len(vector) != 768 or not all(math.isfinite(v) for v in vector)
                        or sum(v * v for v in vector) == 0):
                    raise ValueError("Invalid embedding vector")
            return vectors
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise EmbeddingError("Local embedding generation failed") from exc


@lru_cache
def get_embedder() -> OllamaEmbedder:
    return OllamaEmbedder()
