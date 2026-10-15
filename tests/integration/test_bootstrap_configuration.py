"""Начальные данные каталога конфигурируются и не перезаписывают правки владельца."""

import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce, bootstrap
from sunrise_infrastructure.db import Account, Agreement, Base, Database, Job, Product, Segment, Setting, now
from sunrise_infrastructure.recurring import Recurring

from tests.legal_fixture import accept_test_legal


@pytest.fixture
def empty_database():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Требуется отдельная синтетическая PostgreSQL")
    parsed = make_url(url)
    assert parsed.database == "sunrise_test_backend"
    assert parsed.host in {"127.0.0.1", "localhost"} and parsed.port == 55439
    schema = "bootstrap_test_" + uuid4().hex
    base = Database(url)
    with base.engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped = parsed.set(query={**parsed.query, "options": "-csearch_path=" + schema})
    db = Database(scoped.render_as_string(hide_password=False))
    try:
        Base.metadata.create_all(db.engine)
        yield db
    finally:
        db.engine.dispose()
        assert schema.startswith("bootstrap_test_") and len(schema) == 47
        with base.engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        base.engine.dispose()


def custom_seed():
    return {
        "products": [
            {
                "id": "custom-product",
                "code": "custom-product",
                "name": "Свой тариф",
                "description": "Описание владельца",
                "tier_level": 7,
                "price": "137.42",
                "currency": "RUB",
                "included_devices": 9,
                "extra_device_price": "23.11",
                "presentation": {},
            }
        ],
        "settings": {
            "recurring": {"enabled": False},
            "periods": [{"days": 42, "months": 1, "discount_percent": "3"}],
            "discounts": {"cap": "27", "bonus_cap": "19", "secondary": [], "referral_percent": "8"},
        },
    }


def test_explicit_seed_supplies_all_catalog_business_values(empty_database):
    bootstrap(empty_database, seed=custom_seed())
    from sunrise_api.schemas import ProductDto

    public_product = Commerce(empty_database, local_test=True).catalog()["products"][0]
    assert ProductDto.model_validate(public_product).base_period_days == 42
    with empty_database.transaction() as session:
        product = session.scalar(select(Product))
        assert product.id == "custom-product" and product.name == "Свой тариф"
        assert product.price == Decimal("137.42")
        assert product.included_devices == 9
        assert product.extra_device_price == Decimal("23.11")
        assert session.get(Setting, "periods").value[0]["days"] == 42
        assert session.get(Setting, "discounts").value["referral_percent"] == "8"


def test_repeated_bootstrap_preserves_owner_catalog_and_policies(empty_database):
    bootstrap(empty_database)
    with empty_database.transaction() as session:
        product = session.scalar(select(Product).order_by(Product.id))
        product.name = "Изменено владельцем"
        product.price = Decimal("321.45")
        session.get(Setting, "discounts").value = {"cap": "22", "bonus_cap": "11", "secondary": []}
        before_id = product.id
    bootstrap(empty_database, seed=custom_seed())
    with empty_database.transaction() as session:
        product = session.get(Product, before_id)
        assert product.name == "Изменено владельцем" and product.price == Decimal("321.45")
        assert session.get(Setting, "discounts").value["cap"] == "22"
        assert session.get(Product, "custom-product") is None


def test_invalid_seed_is_rejected_without_partial_database_writes(empty_database):
    data = custom_seed()
    data["products"][0]["price"] = "-10.00"
    with pytest.raises(ValueError):
        bootstrap(empty_database, seed=data)
    with empty_database.transaction() as session:
        assert session.scalar(select(Product)) is None
        assert session.scalar(select(Setting)) is None


@pytest.mark.parametrize(
    "policy",
    [
        {"referral_percent": "NaN"},
        {"secondary": ["101"]},
        {"loyalty_period_days": 0, "loyalty_milestones": []},
        {"loyalty_period_days": 30, "loyalty_milestones": [{"elapsed_periods": 2, "percent": "-1"}]},
    ],
)
def test_seed_rejects_invalid_reward_policies_atomically(empty_database, policy):
    data = custom_seed()
    data["settings"]["discounts"].update(policy)
    with pytest.raises(ValueError):
        bootstrap(empty_database, seed=data)
    with empty_database.transaction() as session:
        assert session.scalar(select(Product)) is None


def test_missing_discount_policy_cannot_invent_price_terms(empty_database):
    bootstrap(empty_database)
    with empty_database.transaction() as session:
        account = Account(email="policy@example.test", email_verified=True)
        session.add(account)
        session.flush()
        account_id = account.id
        session.delete(session.get(Setting, "discounts"))
    commerce = Commerce(empty_database, local_test=True)
    accept_test_legal(commerce, account_id)
    with pytest.raises(DomainError) as failure:
        commerce.quote(account_id, product_id="standard", period_days=30, action="purchase")
    assert failure.value.code == "pricing_policy_unavailable"


def test_missing_referral_policy_does_not_offer_an_invented_reward(empty_database):
    bootstrap(empty_database, seed=custom_seed())
    with empty_database.transaction() as session:
        account = Account(email="referral@example.test")
        session.add(account)
        session.flush()
        account_id = account.id
        session.get(Setting, "discounts").value = {"cap": "27", "bonus_cap": "19", "secondary": []}
    result = Commerce(empty_database, local_test=True).referral_info(account_id)
    assert result["enrollment_available"] is False
    assert result["reward_percent"] == "0"


def test_missing_loyalty_policy_never_invents_milestones(empty_database):
    bootstrap(empty_database, seed=custom_seed())
    commerce = Commerce(empty_database, local_test=True)
    with empty_database.transaction() as session:
        account = Account(email="loyalty@example.test", email_verified=True)
        session.add(account)
        session.flush()
        account_id = account.id
    accept_test_legal(commerce, account_id)
    quotation = commerce.quote(account_id, product_id="custom-product", period_days=42, action="purchase")
    order = commerce.create_order(account_id, quotation["id"], "loyalty-policy-order")
    commerce.test_pay(account_id, order["id"])
    subscription_id = commerce.dashboard(account_id)["subscriptions"][0]["id"]
    first = now() - timedelta(days=70)
    with empty_database.transaction() as session:
        segment = session.scalar(select(Segment).where(Segment.subscription_id == subscription_id))
        segment.starts_at, segment.ends_at = first, now() + timedelta(days=20)
        session.add(
            Agreement(
                account_id=account_id,
                subscription_id=subscription_id,
                state="stopped",
                consent_amount=Decimal("137.42"),
                currency="RUB",
                consent={"continuous_paid_since": first.isoformat()},
            )
        )
    result = Recurring(commerce, None).loyalty(subscription_id)
    assert result["percent"] == "0"
    assert result["policy_configured"] is False


def test_legacy_mail_job_uses_configured_instance_subject(empty_database):
    from sunrise_infrastructure.instance_config import InstanceConfig
    from sunrise_infrastructure.mail import SMTPTestMail

    from tests.contract.test_mail_templates import FakeSMTP

    smtp = FakeSMTP()
    instance = InstanceConfig(app_name="Другой магазин")
    transport = SMTPTestMail(
        host="smtp.example.test",
        port=587,
        mode="starttls",
        sender="sender@example.test",
        allowed_hosts={"smtp.example.test"},
        allowed_recipients={"payer@example.test"},
        authorized=True,
        factory=lambda **_: smtp,
        instance=instance,
    )
    with empty_database.transaction() as session:
        account = Account(email="payer@example.test", email_verified=True)
        session.add(account)
        session.flush()
        session.add(
            Job(
                key="synthetic-instance-mail",
                kind="broadcast",
                account_id=account.id,
                payload={"nodes": [{"type": "text", "text": "Сообщение владельца"}]},
            )
        )
    commerce = Commerce(
        empty_database,
        local_test=True,
        public_settings=SimpleNamespace(instance=instance),
        mail_transport=transport,
        notification_allowlist=["payer@example.test"],
    )
    assert commerce.run_jobs() == 1
    assert smtp.messages[0][0]["Subject"] == "Сообщение Другой магазин"


def test_confirmed_payment_survives_missing_referral_policy_and_manual_recovery(empty_database):
    from sunrise_infrastructure.billing import Billing, VerifiedPayment
    from sunrise_infrastructure.db import Audit, Ledger, Order, Payment, Quote, Subscription

    bootstrap(empty_database, seed=custom_seed())
    commerce = Commerce(empty_database, local_test=True)
    with empty_database.transaction() as session:
        referrer = Account(email="ref-owner@example.test")
        session.add(referrer)
        session.flush()
        buyer = Account(email="buyer@example.test", referrer_id=referrer.id, bonus_balance=Decimal("100"))
        session.add(buyer)
        session.flush()
        buyer_id, referrer_id = buyer.id, referrer.id
    accept_test_legal(commerce, buyer_id)
    quotation = commerce.quote(buyer_id, "custom-product", 42, "purchase", bonus_amount="5")
    order = commerce.create_order(buyer_id, quotation["id"], "ref-policy-payment")
    with empty_database.transaction() as session:
        policy = session.get(Setting, "discounts")
        policy.value = {key: value for key, value in policy.value.items() if key != "referral_percent"}
    fact = VerifiedPayment(
        provider="fixture",
        provider_id="verified-ref-policy",
        terminal="test-terminal",
        environment="local_test",
        order_id=order["id"],
        amount=Decimal(order["total"]),
        currency="RUB",
        paid_at=now(),
        fee=Decimal("1.25"),
        fee_currency="RUB",
    )
    billing = Billing(commerce, "test-terminal", "local_test")
    try:
        assert billing.accept(fact)["status"] == "reconciliation_required"
    except DomainError:
        pytest.fail("Confirmed payment fact was rolled back for a missing referral policy")
    assert billing.accept(fact)["status"] == "reconciliation_required"
    with empty_database.transaction() as session:
        assert len(list(session.scalars(select(Payment)))) == 1
        assert session.scalar(select(Payment)).fee == Decimal("1.25")
        assert session.scalar(select(Subscription)) is None
        assert session.scalar(select(Ledger)) is None
        assert session.get(Account, buyer_id).bonus_reserved == Decimal("5")
        assert session.get(Account, buyer_id).bonus_balance == Decimal("100")
        issue = session.scalar(select(Audit).where(Audit.action == "fulfillment_policy_blocked"))
        assert issue and issue.details["code"] == "referral_policy_unavailable"
        policy = session.get(Setting, "discounts")
        policy.value = policy.value | {"referral_percent": "8"}
    # Explicit internal reconciliation, using the already recorded payment, never a new debit.
    with empty_database.transaction() as session:
        buyer = commerce._account(session, buyer_id, True)
        saved_order = session.get(Order, order["id"])
        saved_payment = session.scalar(select(Payment))
        commerce._fulfill(
            session, buyer, saved_order, session.get(Quote, saved_order.quote_id), saved_payment.id
        )
    assert billing.accept(fact)["status"] == "paid"
    with empty_database.transaction() as session:
        assert len(list(session.scalars(select(Payment)))) == 1
        assert len(list(session.scalars(select(Subscription)))) == 1
        assert session.get(Account, buyer_id).bonus_balance == Decimal("95")
        assert session.get(Account, referrer_id).bonus_balance == (fact.amount * Decimal("0.08")).quantize(
            Decimal("0.01")
        )


def test_fractional_json_seed_percentages_keep_decimal_money_exact(empty_database):
    seed = custom_seed()
    seed["products"][0]["price"] = "9999999999999999.00"
    seed["settings"]["discounts"].update(cap=27.1, bonus_cap=19.1, secondary=[27.1], referral_percent=8.1)
    bootstrap(empty_database, seed=seed)
    commerce = Commerce(empty_database, local_test=True)
    with empty_database.transaction() as session:
        referrer = Account(email="fraction-referrer@example.test")
        session.add(referrer)
        session.flush()
        account = Account(email="fraction-buyer@example.test", referrer_id=referrer.id)
        session.add(account)
        session.flush()
        account_id, referrer_id = account.id, referrer.id
    accept_test_legal(commerce, account_id)
    quotation = commerce.quote(account_id, "custom-product", 42, "purchase")
    expected = (Decimal("9999999999999999.00") * Decimal("0.729")).quantize(Decimal("0.01"))
    assert Decimal(quotation["total"]) == expected
    order = commerce.create_order(account_id, quotation["id"], "fractional-seed-order")
    commerce.test_pay(account_id, order["id"])
    with empty_database.transaction() as session:
        policy = session.get(Setting, "discounts").value
        assert policy["cap"] == "27.1" and policy["secondary"] == ["27.1"]
        assert session.get(Account, referrer_id).bonus_balance == (expected * Decimal("0.081")).quantize(
            Decimal("0.01")
        )


def test_explicit_missing_policy_backfill_preserves_existing_owner_choices(empty_database):
    import sunrise_infrastructure.bootstrap_config as module

    bootstrap(empty_database)
    with empty_database.transaction() as session:
        session.get(Setting, "discounts").value = {
            "cap": "22",
            "bonus_cap": "11",
            "secondary": [],
            "promos": [{"code": "OWNER-DEFINED"}],
        }
    updater = getattr(module, "apply_missing_policy_fields", None)
    assert updater is not None, "Missing explicit policy upgrade utility"
    assert updater(empty_database) == ["loyalty_milestones", "loyalty_period_days", "referral_percent"]
    assert updater(empty_database) == []
    with empty_database.transaction() as session:
        policy = session.get(Setting, "discounts").value
        assert policy["cap"] == "22" and policy["bonus_cap"] == "11"
        assert policy["promos"] == [{"code": "OWNER-DEFINED"}]
        assert policy["referral_percent"] == "15"


def test_admin_price_preview_has_no_invented_discount_caps(empty_database):
    from sunrise_infrastructure.admin import Admin

    bootstrap(empty_database, seed=custom_seed())
    with empty_database.transaction() as session:
        session.delete(session.get(Setting, "discounts"))
    admin = Admin(Commerce(empty_database, local_test=True), None)
    with pytest.raises(DomainError) as failure:
        admin.discount_preview({"product_id": "custom-product", "period_days": 42})
    assert failure.value.code == "pricing_policy_unavailable"


def test_fractional_periods_preserve_decimal_input():
    from sunrise_infrastructure.bootstrap_config import load_bootstrap_seed

    seed = custom_seed()
    seed["settings"]["periods"] = [{"days": 42, "months": 1.1, "discount_percent": 7.1}]
    period = load_bootstrap_seed(seed).settings.periods[0]
    assert period["months"] == "1.1"
    assert period["discount_percent"] == "7.1"
    assert seed["settings"]["periods"][0]["months"] == 1.1


@pytest.mark.parametrize("value", [None, "broken", float("nan"), float("inf")])
def test_invalid_period_values_are_validation_errors(value):
    from pydantic import ValidationError
    from sunrise_infrastructure.bootstrap_config import load_bootstrap_seed

    seed = custom_seed()
    seed["settings"]["periods"][0]["months"] = value
    with pytest.raises(ValidationError):
        load_bootstrap_seed(seed)


def test_admin_preview_matches_quote_for_legacy_numeric_period(empty_database):
    from sunrise_infrastructure.admin import Admin

    seed = custom_seed()
    seed["products"][0]["price"] = "9999999999999999.00"
    bootstrap(empty_database, seed=seed)
    with empty_database.transaction() as session:
        session.get(Setting, "periods").value = [{"days": 42, "months": 1.1, "discount_percent": 27.1}]
        session.get(Setting, "discounts").value = {"cap": "99", "bonus_cap": "50", "secondary": []}
        account = Account(email="period-preview@example.test")
        session.add(account)
        session.flush()
        account_id = account.id
    commerce = Commerce(empty_database, local_test=True)
    accept_test_legal(commerce, account_id)
    preview = Admin(commerce, None).discount_preview({"product_id": "custom-product", "period_days": 42})
    quote = commerce.quote(account_id, "custom-product", 42, "purchase")
    expected = (Decimal("9999999999999999.00") * Decimal("1.1") * Decimal("0.729")).quantize(Decimal("0.01"))
    assert Decimal(preview["external"]) == expected
    assert Decimal(quote["total"]) == expected
    undiscounted = Admin(commerce, None).discount_preview(
        {"product_id": "custom-product", "period_days": 42, "period_discount_percent": "0"}
    )
    assert Decimal(undiscounted["period_discount_percent"]) == 0


@pytest.mark.parametrize("missing_field", ["included_devices", "currency"])
def test_direct_catalog_create_rejects_missing_instance_values(empty_database, missing_field):
    from sunrise_infrastructure.admin import Admin

    values = {
        "code": "explicit",
        "name": "Явные условия",
        "description": "Описание",
        "tier_level": 1,
        "price": "100.00",
        "currency": "RUB",
        "included_devices": 3,
    }
    del values[missing_field]
    with pytest.raises(DomainError) as failure:
        Admin(Commerce(empty_database, local_test=True), None).catalog_create(None, values, "Настройка")
    assert failure.value.code == "catalog_invalid" and failure.value.status == 400


def test_unconfigured_panel_mapping_has_no_private_project_prefix(empty_database):
    from sunrise_infrastructure.admin import Admin

    view = Admin(Commerce(empty_database, local_test=True), None).panel_mapping()
    assert view == {"external_squad_uuid": None, "project_prefix": ""}
