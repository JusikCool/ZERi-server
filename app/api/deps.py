"""Shared FastAPI dependencies.

Phase 0 only re-exports the DB session dependency. Auth dependencies (current_user,
optional_user, etc.) are added in Phase 1 by the auth lead.
"""

from app.db.session import get_db

__all__ = ["get_db"]
