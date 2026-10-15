"""Числовые исторические JSON-настройки не ломают публичный контракт каталога."""

from copy import deepcopy
from decimal import Decimal

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sunrise_api.main import create_app
from sunrise_api.schemas import QuoteDto
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Product, Quote, Setting
from sunrise_infrastructure.settings import Settings

from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


@pytest.mark.parametrize(
    "months,expected_months,expected_price",
    [(1, 1, "5.00"), (1.1, "1.1", "5.49")],
)
@pytest.mark.parametrize("product_override", [False, True])
def test_public_catalog_normalizes_numeric_periods_without_rewriting_storage(
    months, expected_months, expected_price, product_override
):
    with isolated_database("catalog_response") as db:
        raw = [{"days": 42, "months": months, "discount_percent": 0.1}]
        with db.transaction() as session:
            session.get(Setting, "periods").value = deepcopy(raw)
            product = session.scalars(select(Product).order_by(Product.id)).first()
            product.price = Decimal("5.00")
            product.presentation = {"periods": deepcopy(raw)} if product_override else {}
            product_id = product.id
        settings = Settings(
            _env_file=None,
            secret_key="catalog-response-test-secret-" * 2,
            database_url=db.engine.url.render_as_string(hide_password=False),
            local_test=True,
            instance_config_path=None,
            allowed_hosts=["testserver"],
        )
        app = create_app(settings, redis_client=fakeredis.FakeRedis())
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/catalog")
            assert response.status_code == 200
            payload = response.json()
            global_period = payload["periods"][0]
            rendered = next(item for item in payload["products"] if item["id"] == product_id)["periods"][0]
            for period in (global_period, rendered):
                assert period["months"] == expected_months
                assert period["discount_percent"] == "0.1"
            assert rendered["price"] == expected_price
            with db.transaction() as session:
                assert session.get(Setting, "periods").value == raw
                assert session.get(Product, product_id).presentation == (
                    {"periods": raw} if product_override else {}
                )
        finally:
            app.state.commerce.db.engine.dispose()


@pytest.mark.parametrize("months", [1.1, "1.1"])
def test_fractional_renewal_quote_response_preserves_stored_months_and_paid_rights(months):
    with isolated_database("renewal_response") as db:
        with db.transaction() as session:
            session.get(Setting, "periods").value = [
                {"days": 42, "months": months, "discount_percent": "0.1"}
            ]
            account = Account(email="renewal-response@example.test", email_verified=True)
            session.add(account)
            session.flush()
            account_id = account.id
        commerce = Commerce(db, local_test=True)
        accept_test_legal(commerce, account_id)
        purchase = commerce.quote(account_id, "standard", 42, "purchase", extra_devices=1)
        order = commerce.create_order(account_id, purchase["id"], "synthetic-response-purchase")
        commerce.test_pay(account_id, order["id"])
        before = commerce.dashboard(account_id)["subscriptions"][0]
        renewal = commerce.quote(account_id, "standard", 42, "renew", subscription_id=before["id"])
        with db.transaction() as session:
            stored = deepcopy(session.get(Quote, renewal["id"]).snapshot)
        validated = QuoteDto.model_validate(renewal)
        assert validated.device_pricing.billing_months == "1.1"
        assert stored["device_pricing"]["billing_months"] == months
        with db.transaction() as session:
            assert session.get(Quote, renewal["id"]).snapshot == stored
        assert commerce.dashboard(account_id)["subscriptions"][0] == before
