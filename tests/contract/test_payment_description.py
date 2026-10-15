"""Invoice text reaches the synthetic HTTP boundary from saved order facts."""

import json
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.db import Order, Quote, now
from sunrise_infrastructure.providers import RollyPay


def invoice(snapshot, *, source="bot", account=None, prior=None):
    order = Order(
        id="own-order", account_id=9007199254740993, quote_id="quote", source=source,
        environment="sandbox", amount=Decimal("12.34"), currency="RUB", status="awaiting_payment",
    )
    quote = Quote(id="quote", expires_at=now() + timedelta(minutes=1), snapshot=snapshot)
    account = account or SimpleNamespace(
        id=order.account_id, telegram_id=1234567890123, telegram_username="example_buyer",
        email="unused@example.test", email_verified=True,
    )
    rows, requests = ([prior] if prior else []), []
    session = SimpleNamespace(
        scalar=lambda query: (
            order if query.column_descriptions[0].get("entity") is Order else rows[0] if rows else None
        ),
        get=lambda model, key, **_: order if model is Order else quote if model is Quote else rows[0],
        add=rows.append, flush=lambda: setattr(rows[0], "id", "operation"),
    )

    @contextmanager
    def transaction():
        yield session

    commerce = SimpleNamespace(
        db=SimpleNamespace(transaction=transaction),
        public_settings=SimpleNamespace(
            bot_url="https://t.me/configured_example_bot", dashboard_url="https://cabinet.example.test",
            instance=SimpleNamespace(
            telegram_bot_username="configured_example_bot", dashboard_url="https://cabinet.example.test"
        )),
        _account=lambda *args, **kwargs: account,
        _order_dto=lambda _: {"checkout_url": rows[0].pay_url},
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "order_id": order.id, "payment_id": "synthetic-payment", "test": True,
            "environment": "sandbox", "pay_url": "https://pay.rollypay.io/fixture",
        })

    provider = RollyPay(
        "https://payments.example.test", "synthetic", "terminal", {"payments.example.test"},
        transport=httpx.MockTransport(handler),
    )
    billing = Billing(commerce, "terminal", "sandbox")
    billing._notify_checkout_ready = lambda *args: None
    billing.prepare_payment(order.id, provider)
    snapshot["product_name"] = "Renamed after invoice"
    account.telegram_username = "changed_username"
    billing.prepare_payment(order.id, provider)
    assert order.status == "awaiting_payment"
    return rows, requests


@pytest.mark.parametrize(("action", "label"), [
    ("purchase", "New"), ("renew", "Renew"), ("upgrade", "Upgrade"),
    ("downgrade", "Downgrade"), ("devices", "Devices"),
])
@pytest.mark.parametrize("source", ["bot", "site"])
def test_invoice_uses_snapshot_action_period_emoji_and_canonical_buyer(action, label, source):
    rows, requests = invoice({
        "action": action, "product_name": "Outdated plain caption", "period_days": 90,
        "product_content_name": [
            {"type": "emoji", "id": "123456", "fallback": "⬜️", "label": "Тариф"},
            {"type": "text", "text": " Универсальный"},
        ],
    }, source=source)
    site = " Site: cabinet.example.test" if source == "site" else ""
    expected = (
        f"{label}: ⬜️ Универсальный (90d) Bot: configured_example_bot{site} "
        "Buyer: example_buyer (ID: 1234567890123)"
    )
    assert len(requests) == 1
    assert requests[0]["description"] == expected
    assert rows[0].request["description"] == expected
    assert "product_content_name" not in requests[0]
    assert requests[0]["amount"] == "12.34"


@pytest.mark.parametrize(("telegram_id", "username", "verified", "identity"), [
    (1234567890123, None, True, "ID: 1234567890123"),
    (None, "untrusted_claim", True, "buyer@example.test"),
    (None, "untrusted_claim", False, "Telegram не привязан"),
])
def test_invoice_uses_only_bound_telegram_or_verified_email(telegram_id, username, verified, identity):
    _, requests = invoice({"action": "renew", "product_name": "Тариф", "basis": {"period_days": 30}},
                          account=SimpleNamespace(id=9007199254740993, telegram_id=telegram_id,
                              telegram_username=username, email="buyer@example.test", email_verified=verified))
    text = requests[0]["description"]
    assert text.endswith("Buyer: " + identity)
    assert "(30d)" in text
    assert "9007199254740993" not in text and "untrusted_claim" not in text


def test_long_markup_product_is_plain_single_line_and_preserves_long_verified_email():
    email = "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 56 + ".test"
    _, requests = invoice({"action": "purchase", "period_days": 365,
        "product_name": '<b><tg-emoji emoji-id="123">⬜️</tg-emoji> Тариф</b>\n\t\x00\u202e' + "Ж" * 1000},
        source="site", account=SimpleNamespace(id=999, telegram_id=None, telegram_username="claimed",
                                              email=email, email_verified=True))
    text = requests[0]["description"]
    assert len(text) <= 512 and text.endswith("Buyer: " + email)
    assert text.startswith("New: ⬜️ Тариф")
    assert "(365d)" in text and "Site: cabinet.example.test" in text
    assert not any(c in text for c in "<>\n\t\x00\u202e")


def test_old_saved_operation_without_description_is_not_recreated():
    prior = SimpleNamespace(state="outcome_unknown", request={"order_id": "own-order"}, pay_url=None)
    rows, requests = invoice({}, prior=prior)
    assert requests == [] and rows == [prior]
    assert rows[0].request == {"order_id": "own-order"}


def test_optional_transport_description_keeps_legacy_payload_compatible():
    bodies = []
    provider = RollyPay("https://payments.example.test", "synthetic", "terminal",
        {"payments.example.test"}, transport=httpx.MockTransport(
            lambda request: bodies.append(json.loads(request.content)) or httpx.Response(200, json={})))
    provider.create_payment("order", "12.34", "RUB", "account")
    assert "description" not in bodies[0]
    provider.create_payment("order2", "12.34", "RUB", "account", description="New: Тариф")
    assert bodies[1]["description"] == "New: Тариф"
