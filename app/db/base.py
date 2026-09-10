"""Metadata target for Alembic.

Importing `app.models` pulls every model into `Base.metadata`, which is what
autogenerate diffs against. Alembic imports this module and nothing else.
"""

import app.models
from app.models import Base

target_metadata = Base.metadata

__all__ = ["Base", "target_metadata"]
