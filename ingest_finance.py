"""Validate CSV/notes, embed locally, and commit the complete import atomically."""
import argparse
import csv
import hashlib
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database import get_engine, verify_embedding_config
from embeddings import OllamaEmbedder
from models import FinancialDocumentChunk


def make_record(path: Path, location: int, content: str, metadata: dict) -> dict:
    # Row/chunk location preserves legitimate identical transactions within a file.
    key = f"{path.resolve()}\0{location}\0{content}\0{metadata}"
    return {"content": content, "document_metadata": metadata,
            "fingerprint": hashlib.sha256(key.encode("utf-8")).hexdigest()}


def parse_csv(path: Path, source_type: str = "bank_statement") -> list[dict]:
    records = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"Date", "Description", "Amount", "Category"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{path.name}: required columns: {', '.join(sorted(required))}")
        for number, row in enumerate(reader, start=2):
            try:
                if None in row:
                    raise ValueError("Unexpected extra columns")
                day = date.fromisoformat(row["Date"].strip()).isoformat()
                merchant = row["Description"].strip()
                category = row["Category"].strip()
                raw_amount = Decimal(row["Amount"].strip())
                if (not raw_amount.is_finite() or abs(raw_amount) >= Decimal("1e16")
                        or raw_amount != raw_amount.quantize(Decimal("0.01"))):
                    raise ValueError("Amount must be finite with at most two decimal places")
                amount = format(raw_amount.quantize(Decimal("0.01")), ".2f")
                if not merchant or len(merchant) > 1500 or len(category) > 100:
                    raise ValueError("Description/category is missing or too long")
            except (ValueError, InvalidOperation, AttributeError, TypeError) as exc:
                # Do not put transaction contents into logs/error messages.
                raise ValueError(f"{path.name}: invalid CSV row {number}") from exc
            metadata = {"source_type": source_type, "source": path.name,
                        "date": day, "amount": amount, "category": category,
                        "merchant": merchant}
            content = f"Date: {day}; Merchant: {merchant}; Amount: {amount}; Category: {category}"
            records.append(make_record(path, number, content, metadata))
    return records


def parse_notes(path: Path) -> list[dict]:
    content = path.read_text(encoding="utf-8").strip()
    records = []
    # Overlap retains context across boundaries; no silent model truncation.
    for start in range(0, len(content), 1800):
        chunk = content[start:start + 2000]
        records.append(make_record(path, start, chunk, {
            "source_type": "financial_note", "source": path.name, "offset": start,
        }))
        if start + 2000 >= len(content):
            break
    return records


def ingest(records: list[dict], embedder: OllamaEmbedder) -> tuple[int, int]:
    inserted = 0
    identity = embedder.identity
    # Any parse, embedding, or database failure leaves no partial imported rows.
    with Session(get_engine()) as session, session.begin():
        verify_embedding_config(session, identity)
        for start in range(0, len(records), 32):
            batch = records[start:start + 32]
            vectors = embedder.embed([row["content"] for row in batch])
            rows = [dict(row, embedding=vector) for row, vector in zip(batch, vectors)]
            statement = insert(FinancialDocumentChunk).values(rows).on_conflict_do_nothing(
                index_elements=["fingerprint"]).returning(FinancialDocumentChunk.id)
            inserted += len(session.execute(statement).all())
        if embedder.identity != identity:
            raise RuntimeError("Embedding model changed during import; transaction rolled back")
    return inserted, len(records) - inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, action="append", default=[])
    parser.add_argument("--notes", type=Path, action="append", default=[])
    parser.add_argument("--source-type", choices=["bank_statement", "credit_card"],
                        default="bank_statement")
    args = parser.parse_args()
    if not args.csv and not args.notes:
        parser.error("Provide at least one --csv or --notes path")
    try:
        records = [r for p in args.csv for r in parse_csv(p, args.source_type)]
        records += [r for p in args.notes for r in parse_notes(p)]
        if not records:
            parser.error("No records found")
        added, skipped = ingest(records, OllamaEmbedder())
    except Exception:
        # Never expose database connection strings, bind values, or document contents.
        parser.exit(1, "Import failed. Check input format, local model, and initialized database.\n")
    print(f"Inserted {added} chunks; skipped {skipped} duplicates.")


if __name__ == "__main__":
    main()
