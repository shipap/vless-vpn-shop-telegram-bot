"""Временная схема только в отдельной локальной тестовой базе."""

import os
import re
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from sunrise_infrastructure.commerce import bootstrap
from sunrise_infrastructure.db import Base, Database


@contextmanager
def isolated_database(prefix):
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Local synthetic PostgreSQL required")
    parsed = make_url(url)
    assert (
        parsed.database == "sunrise_test_backend"
        and parsed.host in {"127.0.0.1", "localhost"}
        and parsed.port == 55439
    )
    assert re.fullmatch(r"[a-z][a-z0-9_]{0,20}", prefix)
    schema = prefix + "_" + uuid4().hex
    base = Database(url)
    with base.engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped = parsed.set(query={**parsed.query, "options": "-csearch_path=" + schema})
    db = Database(scoped.render_as_string(hide_password=False))
    try:
        Base.metadata.create_all(db.engine)
        bootstrap(db)
        yield db
    finally:
        db.engine.dispose()
        assert schema.startswith(prefix + "_") and len(schema) == len(prefix) + 33
        with base.engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        base.engine.dispose()
