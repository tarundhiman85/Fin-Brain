"""Create agent history/checkpoint tables without modifying existing ledger rows."""
from sqlalchemy import text

from agent.models import AgentConversation, AgentTurn
from database import get_engine


def upgrade(engine=None):
    engine = engine if engine is not None else get_engine()
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(731094821)"))
        AgentConversation.__table__.create(connection, checkfirst=True)
        AgentTurn.__table__.create(connection, checkfirst=True)


if __name__ == "__main__":
    upgrade()
    print("Phase 3 agent tables are ready.")
