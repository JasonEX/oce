"""SQLAlchemy 引擎、会话工厂和 ORM 基类。"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from oce.shared.config import get_settings
from oce.shared.config.settings import DatabaseSettings


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    if settings.is_sqlite:
        return create_async_engine(
            settings.url,
            connect_args={"check_same_thread": False},
            echo=settings.echo,
        )
    return create_async_engine(
        settings.url,
        echo=settings.echo,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_pre_ping=True,
        pool_timeout=5,
        pool_recycle=1800,
    )


engine = create_engine(get_settings().database)
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
Base = declarative_base()
