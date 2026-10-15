import os

from alembic import context
from sqlalchemy import create_engine
from sunrise_infrastructure.db import Base

url = os.environ["SUNRISE_DATABASE_URL"]
engine = create_engine(url, hide_parameters=True)
if (
    engine.url.host not in {"127.0.0.1", "localhost"}
    or engine.url.port != 55439
    or engine.url.database
    not in {"sunrise_test", "sunrise_test_backend", "sunrise_test_bot", "sunrise_legacy"}
):
    raise RuntimeError("Миграция разрешена только в изолированной локальной копии.")
with engine.connect() as connection:
    context.configure(
        connection=connection, target_metadata=Base.metadata, version_table="v5_alembic_version"
    )
    with context.begin_transaction():
        context.run_migrations()
