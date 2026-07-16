"""
Database session management for ORM-backed business tables.

The NL2SQL demo data source is still handled by adapters. This module is only
for application data such as users, tokens and user preferences.
"""
from collections.abc import Generator
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

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
    return f"mysql+pymysql://{username}:{password}@{host}:{port}/{database}?charset={charset}"


engine = create_engine(
    build_database_url(),
    pool_pre_ping=True,
    pool_recycle=3600,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


def init_db() -> None:
    """Create ORM tables if they do not exist."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that provides one database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
