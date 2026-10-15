import os
from uuid import uuid4

from sqlalchemy import BigInteger, Column, MetaData, Numeric, String, Table, insert, select
from sunrise_infrastructure.db import Database
from sunrise_infrastructure.legacy import legacy_account_values


def test_reflected_legacy_account_mapping_is_independent_of_database_column_order():
    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    assert db.engine.url.database == "sunrise_test_backend"
    values = {
        "user_id": 9007199254740997,
        "email": " MAPPING@EXAMPLE.TEST ",
        "telegram_id": 9007199254740998,
        "hashed_password": None,
        "balance": "12.345678",
        "referral_balance": "3.456789",
        "referrer_id": None,
    }
    definitions = [
        ("user_id", BigInteger),
        ("email", String(320)),
        ("telegram_id", BigInteger),
        ("hashed_password", String(100)),
        ("balance", Numeric(24, 6)),
        ("referral_balance", Numeric(24, 6)),
        ("referrer_id", BigInteger),
    ]
    results = []
    with db.engine.begin() as connection:
        for columns in (definitions, list(reversed(definitions))):
            table = Table(
                "test_mapping_" + uuid4().hex,
                MetaData(),
                *(Column(name, kind) for name, kind in columns),
                prefixes=["TEMPORARY"],
                postgresql_on_commit="DROP",
            )
            table.create(connection)
            connection.execute(insert(table), values)
            reflected = Table(table.name, MetaData(), autoload_with=connection)
            row = connection.execute(select(reflected)).mappings().one()
            results.append(legacy_account_values(row))
    assert results[0] == results[1]
    assert results[0][0]["id"] == values["user_id"]
    assert str(results[0][0]["credit_balance"]) == "12.345678"
    db.engine.dispose()
