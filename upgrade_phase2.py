"""Add retry keys and immutable execution receipts to an existing Phase 1 ledger."""
from sqlalchemy import text

from database import get_engine


def upgrade(engine=None) -> None:
    engine = engine if engine is not None else get_engine()
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(731094821)"))
        connection.execute(text("SET LOCAL lock_timeout = '10s'"))
        connection.execute(text(
            "ALTER TABLE virtual_orders ADD COLUMN IF NOT EXISTS request_id UUID"))
        connection.execute(text(
            "ALTER TABLE virtual_orders ADD COLUMN IF NOT EXISTS execution_metadata JSONB"))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_virtual_orders_request_id "
            "ON virtual_orders (request_id)"))


if __name__ == "__main__":
    upgrade()
    print("Phase 2 ledger migration complete.")
