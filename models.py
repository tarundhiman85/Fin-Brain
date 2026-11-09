"""PostgreSQL models. Monetary values use fixed-point NUMERIC, never floats."""
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class EmbeddingConfiguration(Base):
    __tablename__ = "embedding_configuration"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (CheckConstraint("id = 1"),)


class FinancialDocumentChunk(Base):
    __tablename__ = "financial_document_chunks"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # SQLAlchemy reserves 'metadata'; the database column retains the requested name.
    document_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=False)
    __table_args__ = (
        Index("ix_finance_embedding_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"},
              postgresql_with={"m": 16, "ef_construction": 64}),
    )


class VirtualPortfolio(Base):
    __tablename__ = "virtual_portfolios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cash_balance: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, default=Decimal("100000.00"),
        server_default="100000.00",
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (CheckConstraint("cash_balance >= 0"),)


class VirtualPosition(Base):
    __tablename__ = "virtual_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("virtual_portfolios.id", ondelete="RESTRICT"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    buy_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(6), nullable=False, server_default="OPEN")
    __table_args__ = (
        UniqueConstraint("portfolio_id", "ticker"),
        CheckConstraint("shares >= 0 AND buy_price > 0"),
        CheckConstraint("status IN ('OPEN', 'CLOSED')"),
        CheckConstraint("(status = 'OPEN' AND shares > 0) OR (status = 'CLOSED' AND shares = 0)"),
    )


class VirtualOrder(Base):
    __tablename__ = "virtual_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("virtual_portfolios.id", ondelete="RESTRICT"), nullable=False, index=True)
    request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    execution_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    buy_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(4), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(6), nullable=False, server_default="OPEN")
    __table_args__ = (
        Index("ix_virtual_orders_request_id", "request_id", unique=True),
        CheckConstraint("shares > 0 AND buy_price > 0"),
        CheckConstraint("order_type IN ('BUY', 'SELL')"),
        CheckConstraint("status IN ('OPEN', 'CLOSED')"),
    )
