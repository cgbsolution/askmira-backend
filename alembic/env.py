"""Alembic environment.

Runs against the *sync* driver (psycopg2) while the application uses asyncpg.
Migrations are a one-shot batch job — the async machinery buys nothing here and
costs a more fragile setup.
"""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.core.config import settings
from app.db.base import target_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _url() -> str:
    """The sync URL, kept out of alembic.ini so the credential lives in .env.

    Deliberately *not* pushed through `config.set_main_option`. That writes
    into a configparser section, and configparser reads `%` as interpolation
    syntax — so a percent-encoded password (which is what you need the moment
    the password contains an `@` or a `#`) fails with an "invalid interpolation
    syntax" error before a connection is ever attempted. Building the engine
    directly from the URL sidesteps the whole problem.
    """
    return settings.DATABASE_URL_SYNC


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — for reviewing a migration."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Supabase (and most managed Postgres) require TLS. `sslmode=require`
    # encrypts without verifying the server certificate chain, which is what
    # psycopg2 can do without a bundled CA file; a local Postgres ignores it.
    connect_args = {}
    if "supabase" in _url() or settings.is_production:
        connect_args["sslmode"] = "require"

    connectable = create_engine(
        _url(), poolclass=pool.NullPool, connect_args=connect_args
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Catch column type and default drift, not just added/dropped
            # columns — a silently widened Numeric in a payroll table is
            # exactly the kind of change that must show up in a diff.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
