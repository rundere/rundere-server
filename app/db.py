from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings


def _engine_kwargs(url: str) -> dict:
    """Pool settings for a server database. pre_ping drops connections the
    server closed (restarts, idle timeouts) instead of failing a request on
    them; SQLite (dev/tests) has no server and keeps the defaults."""
    if url.startswith("sqlite"):
        return {}
    return {
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout_seconds,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }


engine = create_engine(settings.database_url, future=True,
                       **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session():
    with SessionLocal() as session:
        yield session
