import hashlib
import hmac
import json

import httpx
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.providers import RollyPay
from sunrise_infrastructure.security import verify_callback


def test_raw_body_signature_and_replay_age():
    raw = b'{"test":true}'
    sig = hmac.new(b"synthetic", b"1000." + raw, hashlib.sha256).hexdigest()
    verify_callback(raw, "1000", sig, "synthetic", 1001)
    with pytest.raises(DomainError):
        verify_callback(raw + b" ", "1000", sig, "synthetic", 1001)
    with pytest.raises(DomainError):
        verify_callback(raw, "1000", sig, "synthetic", 1400)


def test_payment_test_true_and_recurring_retry_same_key_new_nonce():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"payment_id": "synthetic", "pay_url": "https://pay.example.test/test"}
        )

    c = RollyPay(
        "https://payments.example.test",
        "synthetic",
        "terminal",
        {"payments.example.test"},
        transport=httpx.MockTransport(handler),
        recurring_authorized=True,
        approved_payer_ids={"payer"},
    )
    c.create_payment("order", "99.00", "RUB", "account")
    assert json.loads(requests[0].content)["test"] is True
    c.create_agreement({"plan_id": "plan", "payer_id": "payer"}, "stable")
    c.create_agreement({"plan_id": "plan", "payer_id": "payer"}, "stable")
    assert requests[1].headers["Idempotency-Key"] == requests[2].headers["Idempotency-Key"] == "stable"
    assert requests[1].headers["X-Nonce"] != requests[2].headers["X-Nonce"]
    assert requests[1].url.path == "/api/v1/subscriptions"


def test_real_agreement_and_nonallowlisted_host_denied():
    with pytest.raises(ValueError):
        RollyPay("https://example.com", "synthetic", "terminal", set())
    c = RollyPay("https://payments.example.test", "synthetic", "terminal", {"payments.example.test"})
    with pytest.raises(DomainError):
        c.create_agreement({"plan_id": "x"}, "key")


def test_authorized_recurring_still_denies_unapproved_payer():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    c = RollyPay(
        "https://payments.example.test",
        "synthetic",
        "terminal",
        {"payments.example.test"},
        transport=httpx.MockTransport(handler),
        recurring_authorized=True,
    )
    with pytest.raises(DomainError):
        c.create_agreement({"plan_id": "x", "payer_id": "unknown"}, "key")
    assert calls == []


def test_eur_requires_explicit_international_card_and_unknown_currency_is_denied():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"payment_id": "synthetic", "pay_url": "https://pay.example.test/test"}
        )

    provider = RollyPay(
        "https://payments.example.test",
        "synthetic",
        "terminal",
        {"payments.example.test"},
        transport=httpx.MockTransport(handler),
    )
    provider.create_payment("order", "5.00", "EUR", "account")
    assert calls[0].get("payment_method") == "intl_card"
    assert calls[0]["test"] is True and calls[0]["payment_currency"] == "EUR"
    with pytest.raises(DomainError):
        provider.create_payment("order", "0.99", "EUR", "account")
    with pytest.raises(DomainError):
        provider.create_payment("order", "5.00", "USD", "account")
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [None, []])
def test_malformed_success_json_has_controlled_unknown_outcome(payload):
    provider = RollyPay(
        "https://payments.example.test",
        "synthetic",
        "terminal",
        {"payments.example.test"},
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=json.dumps(payload), headers={"Content-Type": "application/json"}
            )
        ),
    )
    with pytest.raises(DomainError) as error:
        provider.create_payment("order", "99.00", "RUB", "account")
    assert error.value.code == "provider_outcome_unknown"


@pytest.mark.parametrize("action", ["plans", "agreement", "stop_agreement", "charges", "list_agreements"])
def test_one_time_sandbox_permission_never_authorizes_recurring_transport(action):
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
        method = getattr(provider, action)
        method() if action in {"plans", "list_agreements"} else method("synthetic-agreement")
    assert error.value.code == "recurring_test_not_authorized"
    assert requests == []
