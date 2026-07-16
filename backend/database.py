"""Async database session management for ORM-backed business tables."""
from collections.abc import AsyncGenerator
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.config.config import settings


class Base(DeclarativeBase):
    """Base class for SQLAlchemy ORM models."""


def build_database_url() -> str:
    """Build the SQLAlchemy URL used by the user module."""
    if settings.user_database_url:
        return settings.user_database_url

    username = quote_plus(settings.mysql_user)
    password = quote_plus(settings.mysql_password)
    host = settings.mysql_host
    port = settings.mysql_port
    database = settings.mysql_database
    charset = settings.mysql_charset
    return f"mysql+aiomysql://{username}:{password}@{host}:{port}/{database}?charset={charset}"


async_engine = create_async_engine(
    build_database_url(),
    echo=settings.sql_echo,
    pool_size=settings.mysql_pool_size,
    max_overflow=settings.mysql_max_overflow,
    pool_pre_ping=True,
    pool_recycle=3600,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create ORM tables if they do not exist."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides one database session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
