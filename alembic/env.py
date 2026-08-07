import os

from alembic import context
from sqlalchemy import create_engine

from app.models import Base

target_metadata = Base.metadata


def _url() -> str:
    url = os.environ.get("TX_DATABASE_URL")
    if not url:
        raise SystemExit("TX_DATABASE_URL is not set (config via env vars only)")
    return url


def run_migrations_offline():
    context.configure(url=_url(), target_metadata=target_metadata,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
