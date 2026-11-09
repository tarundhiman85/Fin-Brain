"""Financial retrieval shared by the HTTP API and the local MCP server."""
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from database import verify_embedding_config
from embeddings import OllamaEmbedder
from models import FinancialDocumentChunk


class FinanceSearchError(RuntimeError):
    """Safe, public error for incompatible embedding configurations."""


class FinanceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Query must not be blank")
        return value.strip()


class SearchHit(BaseModel):
    id: UUID
    content: str
    metadata: dict[str, Any]
    similarity: float


class SearchResponse(BaseModel):
    results: list[SearchHit]


def search_financial_documents(
    session: Session, embedder: OllamaEmbedder, request: FinanceQuery,
) -> SearchResponse:
    identity = embedder.identity
    try:
        verify_embedding_config(session, identity)
    except RuntimeError:
        raise FinanceSearchError(
            "Embedding configuration mismatch; initialize matching schema") from None
    vector = embedder.embed([request.query], query=True)[0]
    if embedder.identity != identity:
        raise FinanceSearchError("Embedding model changed; retry with the initialized model")
    session.execute(text("SET LOCAL hnsw.ef_search = 100"))
    distance = FinancialDocumentChunk.embedding.cosine_distance(vector)
    rows = session.execute(
        select(FinancialDocumentChunk, distance.label("distance"))
        .order_by(distance).limit(request.top_k)
    ).all()
    return SearchResponse(results=[
        SearchHit(id=row.id, content=row.content, metadata=row.document_metadata,
                  similarity=max(-1.0, min(1.0, 1.0 - float(score))))
        for row, score in rows
    ])
