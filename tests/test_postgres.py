"""Optional real-pgvector smoke test; requires an initialized TEST_DATABASE_URL."""
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import FinancialDocumentChunk, VirtualPortfolio

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"),
                                reason="TEST_DATABASE_URL not provided")


def test_real_vector_search_dedup_and_cash_constraint():
    engine = create_engine(os.environ["TEST_DATABASE_URL"], hide_parameters=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection) as session:
                    identifier = uuid4()
                    row = dict(id=identifier, content="Integration grocery purchase",
                               document_metadata={"source_type": "bank_statement"},
                               fingerprint=uuid4().hex,
                               embedding=[1.0] + [0.0] * 767)
                    statement = insert(FinancialDocumentChunk).values(**row)
                    session.execute(statement)
                    assert session.scalar(statement.on_conflict_do_nothing(
                        index_elements=["fingerprint"]).returning(
                            FinancialDocumentChunk.id)) is None
                    distance = FinancialDocumentChunk.embedding.cosine_distance(row["embedding"])
                    assert session.scalar(select(distance).where(
                        FinancialDocumentChunk.id == identifier)) == pytest.approx(0)
                    portfolio = session.get(VirtualPortfolio, 1)
                    assert portfolio is not None and portfolio.cash_balance >= Decimal("0")
                    with pytest.raises(IntegrityError):
                        with session.begin_nested():
                            portfolio.cash_balance = Decimal("-1")
                            session.flush()
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
