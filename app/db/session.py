"""Async engine and session factory for Supabase Postgres."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # A full pool, or a full Supabase pooler (which queues a client for up to a
    # minute), fails the request in seconds instead of holding it.
    pool_timeout=settings.DB_POOL_TIMEOUT,
    connect_args={"timeout": settings.DB_POOL_TIMEOUT},
    pool_pre_ping=True,  # Supabase poolers drop idle connections
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. One session per request, rolled back on error.

    Services never open their own session — they receive this one, so a
    request's writes commit or roll back as a single unit. That is what keeps
    an audit-log row from surviving the change it describes.
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
