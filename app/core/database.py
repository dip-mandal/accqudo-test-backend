from pathlib import Path
from typing import AsyncGenerator
import ssl

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)
from sqlalchemy.orm import declarative_base

from app.core.config import settings


# ---------------------------------------------------------
# MySQL SSL configuration
# ---------------------------------------------------------

connect_args = {}

if settings.DATABASE_SSL_CA:
    ca_path = Path(settings.DATABASE_SSL_CA)

    if not ca_path.exists():
        raise RuntimeError(
            f"Database SSL CA certificate not found: {ca_path}"
        )

    ssl_context = ssl.create_default_context(
        cafile=str(ca_path)
    )

    connect_args["ssl"] = ssl_context


# ---------------------------------------------------------
# Database engine
# ---------------------------------------------------------

# Keep the local development pool larger, but use a smaller
# pool in production because Cloud Run can run multiple
# backend instances simultaneously.

if settings.ENVIRONMENT == "production":
    pool_size = 5
    max_overflow = 5
else:
    pool_size = 20
    max_overflow = 10


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=pool_size,
    max_overflow=max_overflow,
    pool_timeout=30,
    connect_args=connect_args,
)


# ---------------------------------------------------------
# Async session factory
# ---------------------------------------------------------

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ---------------------------------------------------------
# SQLAlchemy Base
# ---------------------------------------------------------

Base = declarative_base()


# ---------------------------------------------------------
# Database dependency
# ---------------------------------------------------------

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()