import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.models import Base

config = context.config

# Apply alembic.ini's [loggers] so `alembic upgrade head` prints the
# "Running upgrade X -> Y" INFO lines. disable_existing_loggers=False keeps
# any already-configured app loggers alive if Alembic is run in-process.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

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
