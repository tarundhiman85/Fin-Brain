import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")
os.environ.setdefault("API_KEY", "test-key-" + "x" * 40)

from config import Settings, get_settings
from database import get_session
from embeddings import EmbeddingError, OllamaEmbedder, get_embedder
from ingest_finance import parse_csv, parse_notes
from main import app
from models import FinancialDocumentChunk, VirtualOrder, VirtualPortfolio


@pytest.fixture
def client():
    session = Mock()
    app.dependency_overrides[get_session] = lambda: session
    try:
        with TestClient(app) as client:
            yield client, session
    finally:
        app.dependency_overrides.clear()


def test_authentication_and_validation(client):
    api, _ = client
    for path in ("/api/v1/trading/portfolio", "/api/v1/finance/query"):
        response = api.get(path) if "portfolio" in path else api.post(path, json={"query": "food"})
        assert response.status_code == 401
    headers = {"X-API-Key": get_settings().api_key.get_secret_value()}
    assert api.post("/api/v1/finance/query", headers=headers,
                    json={"query": " ", "top_k": 0}).status_code == 422
    assert api.get("/api/v1/trading/portfolio?portfolio_id=0", headers=headers).status_code == 422


def test_empty_and_missing_portfolio(client):
    from datetime import datetime, timezone
    api, session = client
    session.get.return_value = VirtualPortfolio(
        id=1, cash_balance=Decimal("100000.00"), currency="USD",
        updated_at=datetime.now(timezone.utc))
    session.scalars.return_value.all.return_value = []
    headers = {"X-API-Key": get_settings().api_key.get_secret_value()}
    response = api.get("/api/v1/trading/portfolio", headers=headers)
    assert response.status_code == 200
    assert response.json()["cash_balance"] == "100000.00"
    assert response.json()["holdings"] == []
    session.get.return_value = None
    assert api.get("/api/v1/trading/portfolio", headers=headers).status_code == 404


def test_bank_parser_preserves_decimal_and_duplicates(tmp_path):
    path = tmp_path / "bank.csv"
    path.write_text(
        'Date,Description,Amount,Category\n'
        '2026-10-01,"Shop, Ltd",-12.30,Groceries\n'
        '2026-10-01,"Shop, Ltd",-12.30,Groceries\n', encoding="utf-8")
    rows = parse_csv(path)
    assert rows[0]["document_metadata"]["amount"] == "-12.30"
    assert rows[0]["document_metadata"]["merchant"] == "Shop, Ltd"
    assert rows[0]["fingerprint"] != rows[1]["fingerprint"]
    assert rows == parse_csv(path)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1.001", "1e100", "abc"])
def test_rejects_invalid_money(tmp_path, amount):
    path = tmp_path / "bank.csv"
    path.write_text(f"Date,Description,Amount,Category\n2026-10-01,Shop,{amount},Food\n")
    with pytest.raises(ValueError, match="invalid CSV row"):
        parse_csv(path)


def test_notes_cover_all_content_with_overlap(tmp_path):
    path = tmp_path / "notes.txt"
    value = "a" * 1900 + "UNIQUE_BOUNDARY" + "b" * 2200
    path.write_text(value)
    rows = parse_notes(path)
    assert len(rows) == 3
    assert all(len(row["content"]) <= 2000 for row in rows)
    assert "UNIQUE_BOUNDARY" in rows[0]["content"]
    assert "UNIQUE_BOUNDARY" in rows[1]["content"]
    assert rows[-1]["content"].endswith("b" * 500)


def test_disallows_remote_embedding_hosts():
    with pytest.raises(ValidationError):
        Settings(ollama_url="https://remote.example", _env_file=None)


def test_embedding_prefix_and_vector_validation(monkeypatch):
    seen = []
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["embedding"]})
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * 767]})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert len(OllamaEmbedder().embed(["grocery"], query=True)[0]) == 768
    assert seen[0]["input"] == ["search_query: grocery"]
    assert seen[0]["truncate"] is False


def test_rejects_wrong_embedding_dimension(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={} if request.url.path == "/api/show" else {"embeddings": [[1.0, 2.0]]})), **kwargs))
    with pytest.raises(EmbeddingError):
        OllamaEmbedder().embed(["food"])


def test_schema_compiles_for_postgresql():
    dialect = postgresql.dialect()
    ddl = str(CreateTable(FinancialDocumentChunk.__table__).compile(dialect=dialect))
    assert "VECTOR(768)" in ddl and "JSONB" in ddl
    indexes = [str(CreateIndex(index).compile(dialect=dialect))
               for index in FinancialDocumentChunk.__table__.indexes]
    assert any("USING hnsw" in ddl and "vector_cosine_ops" in ddl for ddl in indexes)
    order_ddl = str(CreateTable(VirtualOrder.__table__).compile(dialect=dialect))
    assert "NUMERIC" in order_ddl and "FOREIGN KEY" in order_ddl

def test_semantic_search_returns_metadata_and_cosine_score(client):
    from uuid import uuid4
    api, session = client
    embedder = Mock()
    embedder.identity = "local-test-digest"
    embedder.embed.return_value = [[1.0] + [0.0] * 767]
    session.get.return_value = Mock(identity="local-test-digest")
    chunk = FinancialDocumentChunk(
        id=uuid4(), content="Grocery purchase",
        document_metadata={"amount": "-12.30", "category": "Groceries"})
    session.execute.return_value.all.return_value = [(chunk, 0.25)]
    app.dependency_overrides[get_embedder] = lambda: embedder
    response = api.post("/api/v1/finance/query",
                        headers={"X-API-Key": get_settings().api_key.get_secret_value()},
                        json={"query": "groceries", "top_k": 1})
    assert response.status_code == 200
    hit = response.json()["results"][0]
    assert hit["similarity"] == 0.75
    assert hit["metadata"]["amount"] == "-12.30"
    embedder.embed.assert_called_once_with(["groceries"], query=True)


def test_model_mismatch_is_rejected_before_embedding(client):
    api, session = client
    embedder = Mock()
    embedder.identity = "new-digest"
    session.get.return_value = Mock(identity="old-digest")
    app.dependency_overrides[get_embedder] = lambda: embedder
    response = api.post("/api/v1/finance/query",
                        headers={"X-API-Key": get_settings().api_key.get_secret_value()},
                        json={"query": "groceries"})
    assert response.status_code == 503
    embedder.embed.assert_not_called()


def test_database_errors_do_not_expose_financial_parameters(client):
    from sqlalchemy.exc import OperationalError
    api, session = client
    session.execute.side_effect = OperationalError(
        "SELECT private", {"secret": "sensitive transaction"}, Exception("private database error"))
    response = api.get("/api/v1/trading/portfolio",
                       headers={"X-API-Key": get_settings().api_key.get_secret_value()})
    assert response.status_code == 503
    assert "sensitive" not in response.text
    assert "private database" not in response.text

def test_database_script_uses_canonical_bootstrap(monkeypatch):
    import runpy
    import database
    bootstrap = Mock()
    monkeypatch.setattr(database, "initialize", bootstrap)
    monkeypatch.setattr("embeddings.OllamaEmbedder", lambda: Mock(identity="local-digest"))
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "database.py"),
                   run_name="__main__")
    bootstrap.assert_called_once_with("local-digest")


def test_secret_generation_refuses_overwrite(tmp_path, monkeypatch):
    from setup_env import main as generate_secrets
    template = Path(__file__).resolve().parents[1] / ".env.example"
    (tmp_path / ".env.example").write_text(template.read_text())
    monkeypatch.chdir(tmp_path)
    generate_secrets()
    generated = (tmp_path / ".env").read_text()
    assert "REPLACE_WITH" not in generated
    values = dict(line.split("=", 1) for line in generated.splitlines() if "=" in line)
    assert len(values["API_KEY"]) == 64
    assert values["APP_DB_PASSWORD"] in values["DATABASE_URL"]
    assert len({values[k] for k in ("API_KEY", "POSTGRES_PASSWORD", "APP_DB_PASSWORD")}) == 3
    with pytest.raises(FileExistsError):
        generate_secrets()
    assert (tmp_path / ".env").read_text() == generated
