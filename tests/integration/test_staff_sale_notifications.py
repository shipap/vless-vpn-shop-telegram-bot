"""Уведомления о выполненном заказе на синтетической изолированной БД."""

import os
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Base,
    Database,
    Notification,
    NotificationDelivery,
    Order,
    Product,
    Quote,
    Setting,
    Subscription,
)
from sunrise_infrastructure.notifications import NotificationService, validate_telegram_content

from tests.legal_fixture import accept_test_legal


@pytest.fixture
def sale_context():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    parsed = make_url(url)
    assert parsed.database == "sunrise_test_backend"
    assert parsed.host in {"127.0.0.1", "localhost"} and parsed.port == 55439
    schema = "staff_sales_" + uuid4().hex
    base = Database(url)
    with base.engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    db = Database(
        parsed.set(query={**parsed.query, "options": "-csearch_path=" + schema}).render_as_string(
            hide_password=False
        )
    )
    config = SimpleNamespace(
        local_test=True,
        secret_key="s" * 48,
        owner_id="701234567890123456",
        admin_telegram_allowlist=["701234567890123456", "701234567890123458"],
        admin_email_allowlist=["staff@example.test", "mailstaff@example.test", "unverified@example.test"],
        email_allowlist=["staff@example.test", "mailstaff@example.test", "buyer@example.test"],
        test_telegram_recipients="701234567890123456,701234567890123457,701234567890123458",
        bot_test_token=None,
        push_private_key=None,
        dashboard_url="http://127.0.0.1:3200",
    )
    try:
        Base.metadata.create_all(db.engine)
        with db.transaction() as session:
            session.add_all(
                [
                    Account(
                        id=9007199254740993,
                        email="buyer@example.test",
                        email_verified=True,
                        telegram_id=701234567890123457,
                        telegram_username="sale_buyer",
                    ),
                    Account(
                        id=101,
                        email="staff@example.test",
                        email_verified=True,
                        telegram_id=701234567890123456,
                    ),
                    Account(id=102, email="mailstaff@example.test", email_verified=True,
                            telegram_id=701234567890123458),
                    Account(id=103, email="unverified@example.test", email_verified=False),
                    Product(
                        id="small",
                        code="small",
                        name="Тариф до изменения",
                        tier_level=1,
                        price=Decimal("123.45"),
                        currency="RUB",
                        included_devices=2,
                        extra_device_price=Decimal("10.00"),
                    ),
                    Product(
                        id="large",
                        code="large",
                        name="Старший тариф",
                        tier_level=2,
                        price=Decimal("300.00"),
                        currency="RUB",
                        included_devices=2,
                        extra_device_price=Decimal("10.00"),
                    ),
                    Setting(
                        key="periods",
                        value=[
                            {"days": 30, "months": "1", "discount_percent": "0"},
                            {"days": 90, "months": "3", "discount_percent": "0"},
                        ],
                    ),
                    Setting(key="discounts", value={"cap": "40", "bonus_cap": "50", "secondary": []}),
                    Setting(key="recurring", value={"enabled": False}),
                ]
            )
        commerce = Commerce(db, local_test=True, public_settings=config)
        accept_test_legal(commerce, "9007199254740993")
        yield commerce, config
    finally:
        db.engine.dispose()
        with base.engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        base.engine.dispose()


def fulfill(commerce, action, product="small", sid=None, source="site", period=30, **options):
    quote = commerce.quote("9007199254740993", product, period, action, sid, **options)
    order = commerce.create_order("9007199254740993", quote["id"], uuid4().hex, source=source)
    commerce.test_pay("9007199254740993", order["id"])
    commerce.test_pay("9007199254740993", order["id"])
    return order["id"]


@pytest.mark.parametrize(
    ("action", "source", "title"),
    [
        ("purchase", "site", "Подписка"),
        ("renew", "site", "Продление"),
        ("renew", "recurring", "Автопродление"),
        ("upgrade", "site", "Повышение тарифа"),
        ("downgrade", "site", "Понижение тарифа"),
        ("devices", "site", "Дополнительные устройства"),
    ],
)
def test_common_fulfillment_classifies_sale_once_and_routes_to_linked_staff(
    sale_context, action, source, title
):
    commerce, _ = sale_context
    sid = None
    product = "small"
    options = {}
    if action != "purchase":
        initial = fulfill(commerce, "purchase", product="large" if action == "downgrade" else "small")
        with commerce.db.transaction() as session:
            sid = session.get(Order, initial).subscription_id
        if action == "upgrade":
            product = "large"
        if action == "devices":
            options["extra_devices"] = 1
    oid = fulfill(commerce, action, product=product, sid=sid, source=source, **options)
    with commerce.db.transaction() as session:
        order = session.get(Order, oid)
        quote = session.get(Quote, order.quote_id)
        rows = list(session.scalars(select(Notification).where(Notification.event_key == "sale:" + oid)))
        assert len(rows) == 2
        assert {row.account_id for row in rows} == {101, 102}
        for row in rows:
            assert row.title == "Новая покупка!"
            assert "Покупатель: @sale_buyer (ID: 701234567890123457)" in row.body
            product_line = f"Товар: {title}: {quote.snapshot['product_name']}"
            if action in {"purchase", "renew"}:
                assert product_line + " (30 дн.)" in row.body
            else:
                assert product_line + "\n" in row.body
                assert "(30 дн.)" not in row.body
                if action == "downgrade":
                    assert "Срок подписки пересчитан." in row.body
                    assert "Срок подписки сохранён." not in row.body
                else:
                    assert "Срок подписки сохранён." in row.body
            assert f"Сумма: {order.amount:.2f} RUB" in row.body
            assert "Бонусы:" not in row.body
            deliveries = list(
                session.scalars(
                    select(NotificationDelivery).where(NotificationDelivery.notification_id == row.id)
                )
            )
            assert [(d.channel, d.recipient, d.state) for d in deliveries] == [
                ("telegram", "701234567890123456", "pending")
                if row.account_id == 101
                else ("telegram", "701234567890123458", "pending")
            ]


def test_sale_keeps_quote_name_and_period_after_catalog_rename_and_delivers_safe_text(sale_context):
    commerce, config = sale_context
    name = '<b>Тариф & "старый"</b>'
    with commerce.db.transaction() as session:
        session.get(Product, "small").name = name
    quote = commerce.quote("9007199254740993", "small", 90, "purchase")
    order = commerce.create_order("9007199254740993", quote["id"], "renamed")
    with commerce.db.transaction() as session:
        product = session.get(Product, "small")
        product.name, product.price, product.included_devices = "Новый тариф", Decimal("800.00"), 7
        account = session.get(Account, 9007199254740993)
        account.telegram_username = "<b>evil</b>"
    commerce.test_pay("9007199254740993", order["id"])
    with commerce.db.transaction() as session:
        subscription = session.get(Subscription, session.get(Order, order["id"]).subscription_id)
        subscription.connection_url = "https://example.test/private-vpn-secret"
    sent, mailed = [], []

    class Mail:
        def deliver(self, recipient, subject, body, identifier):
            mailed.append((recipient, subject, body))

    service = NotificationService(
        commerce.db,
        config,
        mail_transport=Mail(),
        telegram_transport=lambda recipient, payload, identifier: sent.append((recipient, payload)),
    )
    service.deliver_pending(limit=100)
    payload = next(p for recipient, p in sent if recipient == "701234567890123456")
    assert "Товар: Подписка: " + name + " (90 дн.)" in payload["body"]
    assert "Новый тариф" not in payload["body"]
    assert "Сумма: 370.35 RUB" in payload["body"]
    assert "Покупатель: ID: 701234567890123457" in payload["body"]
    assert "private-vpn-secret" not in payload["body"]
    rendered = validate_telegram_content(payload["title"], payload["body"], payload["nodes"])
    assert "<b>Тариф" not in rendered and "&lt;b&gt;Тариф &amp;" in rendered
    assert mailed == []
    assert next(p for recipient, p in sent if recipient == "701234567890123458")["body"] == payload["body"]


def test_email_only_buyer_and_legacy_quote_do_not_invent_catalog_or_telegram_identity(sale_context):
    commerce, _ = sale_context
    with commerce.db.transaction() as session:
        buyer = session.get(Account, 9007199254740993)
        buyer.telegram_id, buyer.telegram_username = None, "stale_username"
    quote = commerce.quote("9007199254740993", "small", 30, "purchase")
    order = commerce.create_order("9007199254740993", quote["id"], "legacy-snapshot")
    with commerce.db.transaction() as session:
        stored = session.get(Quote, quote["id"])
        snapshot = dict(stored.snapshot)
        snapshot.pop("product_name")
        snapshot["basis"] = {key: value for key, value in snapshot["basis"].items() if key != "period_days"}
        stored.snapshot = snapshot
    commerce.test_pay("9007199254740993", order["id"])
    body = NotificationService(commerce.db, commerce.public_settings).list(101)["items"][0]["body"]
    assert "Покупатель: buyer@example.test" in body
    assert "stale_username" not in body
    assert "701234567890123457" not in body
    assert "Товар: Подписка: название отсутствует в снимке заказа" in body
    assert "Тариф до изменения" not in body
    assert "(30 дн.)" not in body
