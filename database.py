"""Connection/session lifecycle and explicit, idempotent Phase 1 bootstrap."""
from functools import lru_cache

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine():
    return create_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True, pool_size=5, max_overflow=5,
        hide_parameters=True, connect_args={"connect_timeout": 10},
    )


def get_session():
    with sessionmaker(bind=get_engine(), expire_on_commit=False)() as session:
        yield session


def verify_embedding_config(session, identity: str) -> None:
    from models import EmbeddingConfiguration
    stored = session.get(EmbeddingConfiguration, 1)
    if stored is None or stored.identity != identity:
        raise RuntimeError("Embedding configuration mismatch; initialize the matching database")


def initialize(identity: str) -> None:
    from models import EmbeddingConfiguration, VirtualPortfolio
    # Serialize concurrent initializers; DDL and seed rows commit atomically.
    with get_engine().begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(731094821)"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(connection)
        from sqlalchemy.orm import Session
        with Session(bind=connection) as session:
            config = session.get(EmbeddingConfiguration, 1)
            if config is None:
                session.add(EmbeddingConfiguration(id=1, identity=identity))
            else:
                verify_embedding_config(session, identity)
            if session.scalar(select(VirtualPortfolio.id).limit(1)) is None:
                session.add(VirtualPortfolio(id=1))
            session.flush()


if __name__ == "__main__":
    from embeddings import OllamaEmbedder
    # Use the canonical module so models and bootstrap share the same Base registry.
    from database import initialize as initialize_schema
    initialize_schema(OllamaEmbedder().identity)
    print("Schema initialized; virtual portfolio 1 is ready.")
