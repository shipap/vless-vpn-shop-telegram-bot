import json
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.db import Order, Quote, now
from sunrise_infrastructure.providers import RollyPay


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("site", "https://dashboard.example.test/account?lang=ru&payment_order=own-order#payments"),
        ("bot", "https://t.me/isolated_example_bot"),
    ],
)
def test_saved_channel_sets_both_invoice_redirects_without_confirming_payment(source, destination):
    order = Order(
        id="own-order",
        account_id=9007199254740993,
        quote_id="quote",
        source=source,
        environment="sandbox",
        amount=Decimal("1.00"),
        currency="RUB",
        status="awaiting_payment",
    )
    quote = Quote(id="quote", expires_at=now() + timedelta(minutes=1), snapshot={})
    rows, requests = [], []
    session = SimpleNamespace(
        scalar=lambda query: (
            order if query.column_descriptions[0].get("entity") is Order else rows[0] if rows else None
        ),
        get=lambda model, key, **_: order if model is Order else quote if model is Quote else rows[0],
        add=rows.append,
        flush=lambda: setattr(rows[0], "id", "operation"),
    )

    @contextmanager
    def transaction():
        yield session

    commerce = SimpleNamespace(
        db=SimpleNamespace(transaction=transaction),
        public_settings=SimpleNamespace(
            dashboard_url="https://dashboard.example.test/account?lang=ru",
            bot_url="https://t.me/isolated_example_bot",
        ),
        _account=lambda *args, **kwargs: SimpleNamespace(id=order.account_id),
        _order_dto=lambda _: {"checkout_url": rows[0].pay_url},
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "order_id": order.id,
                "payment_id": "synthetic-payment",
                "test": True,
                "environment": "sandbox",
                "pay_url": "https://pay.rollypay.io/fixture",
            },
        )

    provider = RollyPay(
        "https://payments.example.test",
        "synthetic",
        "terminal",
        {"payments.example.test"},
        transport=httpx.MockTransport(handler),
    )
    billing = Billing(commerce, "terminal", "sandbox")
    billing._notify_checkout_ready = lambda *args: None
    assert billing.prepare_payment(order.id, provider)["state"] == "created"
    assert requests[0]["success_redirect_url"] == destination
    assert requests[0]["fail_redirect_url"] == destination
    assert rows[0].request["success_redirect_url"] == destination
    assert rows[0].request["fail_redirect_url"] == destination
    assert requests[0]["customer_id"] == "9007199254740993"
    assert order.status == "awaiting_payment"
    assert billing.prepare_payment(order.id, provider)["state"] == "created"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "redirect", ["javascript:alert(1)", "https://user:pass@example.test/", "https://example.test/\n"]
)
def test_unsafe_payment_return_is_rejected_before_http(redirect):
    requests = []
    provider = RollyPay(
        "https://payments.example.test",
        "synthetic",
        "terminal",
        {"payments.example.test"},
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={})
        ),
    )
    with pytest.raises(DomainError) as error:
        provider.create_payment("order", "1.00", "RUB", "account", success_redirect_url=redirect)
    assert error.value.code == "payment_return_unavailable"
    assert requests == []


@pytest.mark.parametrize(
    ("source", "config"),
    [
        ("site", SimpleNamespace(dashboard_url=None)),
        ("bot", SimpleNamespace(bot_url=None, dashboard_url="https://dashboard.example.test")),
        ("site", SimpleNamespace(dashboard_url="https://user:secret@example.test/")),
        ("unknown", SimpleNamespace(dashboard_url="https://dashboard.example.test")),
    ],
)
def test_missing_or_unverified_configured_destination_never_falls_back_to_merchant_settings(source, config):
    billing = Billing(SimpleNamespace(db=None, public_settings=config), "terminal", "sandbox")
    with pytest.raises(DomainError) as error:
        billing._payment_return_urls(Order(id="own-order", source=source))
    assert error.value.code == "payment_return_unavailable"
