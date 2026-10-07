"""Check that every enum the models write exists, label for label, in the database.

    cd backend
    .venv/Scripts/python.exe scripts/check_enums.py            # DATABASE_URL_SYNC
    .venv/Scripts/python.exe scripts/check_enums.py <sync-url>

Alembic autogenerate does not compare enum labels, and the test suite builds its
schema from the models, so a label a migration missed (or added in the wrong
case) only shows up as a 500 the first time the code writes it. Run this
against a database built by `alembic upgrade head`. Exits 1 on any mismatch.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlalchemy as sa
from sqlalchemy import create_engine, text

import app.models  # noqa: F401
from app.core.config import settings
from app.db.base import Base

url = sys.argv[1] if len(sys.argv) > 1 else settings.DATABASE_URL_SYNC
eng = create_engine(url)
with eng.connect() as conn:
    rows = conn.execute(text(
        "select t.typname, e.enumlabel from pg_type t join pg_enum e on e.enumtypid = t.oid"
    )).all()
db: dict[str, set[str]] = {}
for name, label in rows:
    db.setdefault(name, set()).add(label)

problems = 0
seen = set()
for table in Base.metadata.sorted_tables:
    for column in table.columns:
        typ = column.type
        if isinstance(typ, sa.Enum) and typ.native_enum and typ.name:
            expected = set(typ.enums)
            key = (typ.name, tuple(sorted(expected)))
            if key in seen:
                continue
            seen.add(key)
            actual = db.get(typ.name)
            if actual is None:
                print(f"MISSING TYPE {typ.name} ({table.name}.{column.name})")
                problems += 1
                continue
            missing = expected - actual
            if missing:
                print(f"{typ.name} ({table.name}.{column.name}): model writes {sorted(missing)} "
                      f"but the database type has {sorted(actual)}")
                problems += 1
print(f"{problems} problem(s) across {len(seen)} enum types")
sys.exit(1 if problems else 0)
