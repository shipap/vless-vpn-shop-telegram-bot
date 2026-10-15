import inspect
from decimal import Decimal

import httpx
import pytest
from sunrise_core.errors import DomainError
from sunrise_core.pricing import calculate
from sunrise_infrastructure.providers import RollyPay


class SyntheticPolicy:
    production = True

    def __init__(self):
        from types import SimpleNamespace

        self.scope = SimpleNamespace(
            rollypay=SimpleNamespace(origin="https://bank.synthetic.test", terminal="synthetic-terminal")
        )
        self.allowed = True
        self.calls = []

    def require(self, capability=None):
        self.calls.append(capability)
        if not self.allowed:
            from sunrise_core.errors import DomainError

            raise DomainError("production_stage_denied", "Операция недоступна.", 503)

    def require_current(self, session, capability=None):
        return self.require(capability)


@pytest.mark.parametrize("method", ["crypto", "xrocket", "sbp"])
def test_live_invoice_wire_preserves_exact_method_and_bigint_owner(method):
    assert "environment" in inspect.signature(RollyPay).parameters, "Live provider environment is missing"
    import json

    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"environment": "prod", "payment_method": method})

    policy = SyntheticPolicy()
    provider = RollyPay(
        "https://bank.synthetic.test",
        "synthetic-key",
        "synthetic-terminal",
        {"bank.synthetic.test"},
        transport=httpx.MockTransport(handle),
        environment="live",
        runtime_policy=policy,
    )
    provider.create_payment("synthetic-order", "1.00", "RUB", "9007199254740993", payment_method=method)
    assert calls == [
        {
            "amount": "1.00",
            "payment_currency": "RUB",
            "order_id": "synthetic-order",
            "terminal_id": "synthetic-terminal",
            "customer_id": "9007199254740993",
            "test": False,
            "payment_method": method,
        }
    ]
    policy.allowed = False
    with pytest.raises(DomainError):
        provider.create_payment("other", "1.00", "RUB", "9007199254740993", payment_method=method)
    assert len(calls) == 1


def test_method_discount_is_sequential_before_cap_bonus_and_credit():
    assert "payment_discount" in inspect.signature(calculate).parameters, "Method discount factor is missing"
    args = (Decimal(1000), Decimal(0), [Decimal(10)], Decimal(100), Decimal(100), Decimal(0))
    assert calculate(*args, payment_discount=Decimal(4)).external == Decimal("864.00")
    assert calculate(*args[:3], Decimal(12), *args[4:], payment_discount=Decimal(4)).external == Decimal(
        "880.00"
    )
    assert calculate(
        Decimal(1200), Decimal(0), [], Decimal(100), Decimal(100), Decimal(0), payment_discount=Decimal(4)
    ).external == Decimal("1152.00")
    assert calculate(
        Decimal(1200), Decimal(0), [], Decimal(2), Decimal(100), Decimal(0), payment_discount=Decimal(4)
    ).external == Decimal("1176.00")


def test_sandbox_default_wire_stays_test_and_unknown_method_never_posts():
    assert "payment_method" in inspect.signature(RollyPay.create_payment).parameters, (
        "Method binding is missing"
    )
    import json

    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"environment": "sandbox", "test": True, "payment_method": "sbp"})

    provider = RollyPay(
        "https://bank.synthetic.test",
        "synthetic-key",
        "synthetic-terminal",
        {"bank.synthetic.test"},
        transport=httpx.MockTransport(handle),
    )
    provider.create_payment("one", "1.00", "RUB", "1", payment_method="xrocket")
    assert calls[0]["test"] is True and calls[0]["payment_method"] == "xrocket"
    with pytest.raises(DomainError):
        provider.create_payment("two", "1.00", "RUB", "1", payment_method="xrocker")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "discounted,bonus,expected_bonus",
    [("0.00", "0.00", "0.00"), ("100.00", "100.00", "99.00"), ("10.00", "9.99", "9.00")],
)
def test_subscription_minimum_preserves_excess_bonus_instead_of_fabricating_fee(
    discounted, bonus, expected_bonus
):
    from sunrise_core.pricing import Price
    from sunrise_infrastructure import commerce

    assert hasattr(commerce, "subscription_minimum"), "Subscription minimum is missing"
    price = Price(Decimal(100), Decimal(discounted), Decimal(bonus), Decimal(discounted) - Decimal(bonus))
    actual, forced = commerce.subscription_minimum(price, Decimal("1.00"))
    assert forced
    assert actual.external == Decimal("1.00")
    assert actual.bonus == Decimal(expected_bonus)
    assert actual.discounted == max(Decimal(discounted), Decimal("1.00"))


def test_positive_service_funded_by_cash_credit_does_not_force_external_invoice():
    from sunrise_core.pricing import Price
    from sunrise_infrastructure import commerce

    assert hasattr(commerce, "subscription_minimum"), "Subscription minimum is missing"
    price = Price(Decimal(100), Decimal(100), Decimal(0), Decimal(100))
    result, forced = commerce.subscription_minimum(price, Decimal("1.00"))
    assert result.external - Decimal(100) == 0
    assert not forced


@pytest.fixture
def quote_service(monkeypatch):
    """Exercise real quote calculation with synthetic ORM objects, no SQL execution."""
    from contextlib import contextmanager
    from decimal import Decimal as D
    from types import SimpleNamespace

    from sunrise_infrastructure.commerce import Commerce
    from sunrise_infrastructure.db import Account, Product, Quote, Setting
    from sunrise_infrastructure.legal import Legal

    account = Account(
        id=9007199254740993,
        telegram_id=None,
        referrer_id=None,
        bonus_balance=D(1000),
        bonus_reserved=D(0),
        credit_balance=D(1000),
        credit_reserved=D(0),
        credit_currency="RUB",
        pending_promo=None,
    )
    product = Product(
        id="synthetic",
        code="synthetic",
        name="Тестовый тариф",
        description="",
        price=D(1000),
        currency="RUB",
        included_devices=1,
        extra_device_price=D(200),
        available=True,
        version=1,
        presentation={},
    )
    values = {
        "periods": [{"days": 30, "months": 1, "discount_percent": "0"}],
        "discounts": {"cap": "100", "bonus_cap": "100"},
        "payment_methods": {
            "version": 1,
            "minimum_subscription_amount": "1.00",
            "methods": [
                {
                    "id": code,
                    "label": label,
                    "provider": "rollypay",
                    "provider_method": code,
                    "discount_percent": percent,
                    "enabled": True,
                    "currencies": ["RUB"],
                    "mapping_contract": "synthetic-reviewed-v1",
                }
                for code, label, percent in [
                    ("crypto", "Криптовалюта", "4"),
                    ("xrocket", "xRocket", "4"),
                    ("sbp", "СБП", "0"),
                ]
            ],
        },
    }
    added = []

    def scalar(query):
        entity = query.column_descriptions[0].get("entity")
        return account if entity is Account else None

    def get(model, key, **_):
        if model is Setting:
            return SimpleNamespace(value=values[key]) if key in values else None
        return product if model is Product and key == product.id else None

    def flush():
        for item in added:
            if isinstance(item, Quote) and item.id is None:
                item.id = "synthetic-quote"

    session = SimpleNamespace(
        scalar=scalar,
        get=get,
        scalars=lambda _: [product],
        add=added.append,
        flush=flush,
        execute=lambda *_: SimpleNamespace(),
    )

    @contextmanager
    def transaction():
        yield session

    guard_calls = []
    monkeypatch.setattr(Legal, "require_current", lambda self, s, a: guard_calls.append(a.id))
    service = Commerce(SimpleNamespace(transaction=transaction), local_test=True)
    return service, account, values, added, guard_calls


def test_actual_common_quote_method_and_floor_bind_snapshot_and_keep_bonus(quote_service):
    from sunrise_infrastructure.db import Quote

    service, account, values, added, guard_calls = quote_service
    quote = service.quote(
        str(account.id),
        "synthetic",
        30,
        "purchase",
        payment_method="crypto",
        bonus_amount="1000",
        credit_amount="1000",
    )
    assert quote["total"] == "1.00"
    assert quote["bonus_amount"] == "959.00"
    assert quote["credit_amount"] == "0"
    assert quote["payment_method"]["id"] == "crypto"
    assert quote["minimum_external_required"] is True
    assert guard_calls == [account.id]
    saved = next(item for item in added if isinstance(item, Quote))
    assert saved.snapshot["payment_method_policy"]["provider_method"] == "crypto"
    assert "payment_method_policy" not in quote
    assert any(row["label"] == "Минимальная оплата подписки" for row in quote["breakdown"])


def test_actual_common_quote_cash_credit_positive_service_stays_internal(quote_service):
    service, account, _, _, _ = quote_service
    quote = service.quote(
        str(account.id), "synthetic", 30, "purchase", payment_method="sbp", credit_amount="1000"
    )
    assert Decimal(quote["total"]) == 0
    assert quote["minimum_external_required"] is False
    assert Decimal(quote["bonus_amount"]) == 0


def test_omitted_method_cannot_bypass_subscription_floor_with_max_bonus(quote_service):
    service, account, _, _, _ = quote_service
    quote = service.quote(
        str(account.id), "synthetic", 30, "purchase", bonus_amount="1000", credit_amount="1000"
    )
    assert Decimal(quote["total"]) == Decimal("1.00")
    assert Decimal(quote["bonus_amount"]) == Decimal("999.00")
    assert Decimal(quote["credit_amount"]) == 0 and quote["minimum_external_required"] is True


@pytest.mark.parametrize("method", ["crypto", "xrocket"])
def test_method_discount_requires_real_gateway_even_when_cash_credit_covers_service(quote_service, method):
    service, account, _, _, _ = quote_service
    quote = service.quote(
        str(account.id), "synthetic", 30, "purchase", payment_method=method, credit_amount="1000"
    )
    assert Decimal(quote["total"]) == Decimal("1.00")
    assert Decimal(quote["credit_amount"]) == Decimal("959.00")
    assert Decimal(quote["bonus_amount"]) == 0
    assert quote["minimum_external_required"] is True
    assert Decimal(account.credit_balance) == Decimal(1000)


@pytest.mark.parametrize("promo_percent", ["100", "120"])
def test_actual_common_quote_promotional_zero_obeys_minimum_and_existing_cap(
    quote_service, monkeypatch, promo_percent
):
    service, account, values, _, _ = quote_service
    account.pending_promo = {"id": "synthetic-promo", "code": "synthetic"}
    monkeypatch.setattr(
        service,
        "_promo",
        lambda *_: {"code": "synthetic", "reward_type": "percent", "percent": promo_percent},
    )
    quote = service.quote(
        str(account.id), "synthetic", 30, "purchase", payment_method="xrocket", credit_amount="1000"
    )
    assert quote["total"] == "1.00" and quote["minimum_external_required"]
    assert Decimal(quote["bonus_amount"]) == 0 and Decimal(quote["credit_amount"]) == 0
    values["discounts"]["cap"] = "20"
    capped = service.quote(str(account.id), "synthetic", 30, "purchase", payment_method="xrocket")
    assert capped["total"] == "800.00" and not capped["minimum_external_required"]


@pytest.mark.parametrize("method", ["sbp", "crypto", "xrocket"])
def test_fixed_money_promo_exceeds_percentage_cap_and_preserves_one_ruble_floor(
    quote_service, monkeypatch, method
):
    service, account, values, _, _ = quote_service
    values["discounts"]["cap"] = "40"
    account.pending_promo = {"id": "synthetic-money-promo", "code": "MONEY"}
    monkeypatch.setattr(
        service, "_promo", lambda *_: {"code": "MONEY", "reward_type": "fixed_rub", "amount_rub": "2222.00"}
    )
    quote = service.quote(str(account.id), "synthetic", 30, "purchase", payment_method=method)
    assert quote["total"] == "1.00"
    assert quote["promo_code"] == "MONEY"
    assert quote["promo_discount_amount"] == "999.00"
    assert quote["payment_method_discount_amount"] == "0.00"
    assert quote["minimum_external_required"] is True
    assert quote["period_discount_amount"] == "0.00"
    assert quote["secondary_discount_amount"] == "0.00"


@pytest.mark.parametrize(
    "secondary,period,amount,expected",
    [
        ("100", "0", "2222.00", "1.00"),
        ("30", "20", "222.22", "577.78"),
    ],
)
def test_fixed_promo_competes_using_realised_price_not_raw_percent(
    quote_service, monkeypatch, secondary, period, amount, expected
):
    service, account, values, _, _ = quote_service
    values["discounts"].update(cap="40", secondary=[secondary])
    values["periods"][0]["discount_percent"] = period
    account.pending_promo = {"id": "money", "code": "MONEY"}
    monkeypatch.setattr(
        service, "_promo", lambda *_: {"code": "MONEY", "reward_type": "fixed_rub", "amount_rub": amount}
    )
    quote = service.quote(str(account.id), "synthetic", 30, "purchase", payment_method="sbp")
    assert quote["total"] == expected
    assert quote["promo_code"] == "MONEY"


@pytest.mark.parametrize(
    "base,amount,secondary,method,expected,applied",
    [
        ("1000", "390", "40", "xrocket", "585.60", True),
        ("1", "1", "40", "sbp", "1.00", False),
    ],
)
def test_fixed_promo_selects_best_price_after_method_and_minimum(
    quote_service, monkeypatch, base, amount, secondary, method, expected, applied
):
    from sunrise_infrastructure.db import Product

    service, account, values, _, _ = quote_service
    with service.db.transaction() as session:
        session.get(Product, "synthetic").price = Decimal(base)
    values["discounts"].update(cap="40", secondary=[secondary])
    account.pending_promo = {"id": "money", "code": "MONEY"}
    monkeypatch.setattr(
        service, "_promo", lambda *_: {"code": "MONEY", "reward_type": "fixed_rub", "amount_rub": amount}
    )
    quote = service.quote(str(account.id), "synthetic", 30, "purchase", payment_method=method)
    assert quote["total"] == expected
    assert bool(quote["promo_code"]) is applied


@pytest.mark.parametrize(
    "method,reward,period_discount,expected",
    [
        ("sbp", "30.00", "0", {"synthetic": "69.00", "other": "219.00"}),
        ("crypto", "30.00", "0", {"synthetic": "66.24", "other": "210.24"}),
        ("sbp", "30.00", "20", {"synthetic": "49.20", "other": "169.20"}),
        ("xrocket", "2222.00", "0", {"synthetic": "1.00", "other": "1.00"}),
    ],
)
def test_fixed_money_promo_basis_keeps_absolute_reward_for_each_tariff(
    quote_service, monkeypatch, method, reward, period_discount, expected
):
    from sunrise_infrastructure.db import Product

    service, account, values, added, _ = quote_service
    with service.db.transaction() as session:
        current = session.get(Product, "synthetic")
        current.price = Decimal("99")
        other = Product(
            id="other",
            code="other",
            name="Другой тариф",
            price=Decimal("249"),
            currency="RUB",
            included_devices=1,
            extra_device_price=Decimal("200"),
            version=1,
            presentation={},
        )
        session.scalars = lambda _: [current, other]
    values["discounts"]["cap"] = "40"
    values["periods"][0]["discount_percent"] = period_discount
    account.pending_promo = {"id": "money", "code": "MONEY"}
    monkeypatch.setattr(
        service, "_promo", lambda *_: {"code": "MONEY", "reward_type": "fixed_rub", "amount_rub": reward}
    )
    quote = service.quote(str(account.id), "synthetic", 30, "purchase", payment_method=method)
    assert quote["total"] == expected["synthetic"]
    assert added[-1].snapshot["basis"]["products"] == expected


def test_live_authoritative_payment_matches_saved_operation_not_callback_environment():
    from types import SimpleNamespace

    from sunrise_infrastructure.billing import Billing

    policy = SyntheticPolicy()
    policy.scope.scope_id = "synthetic-project"
    billing = Billing(SimpleNamespace(db=None, runtime_policy=policy), "synthetic-terminal", "live")
    saved = {
        "order_id": "own",
        "amount": "1.00",
        "currency": "RUB",
        "account_id": "9007199254740993",
        "environment": "live",
        "provider": "rollypay",
        "terminal": "synthetic-terminal",
        "scope_id": "synthetic-project",
        "payment_method": "xrocket",
    }
    event = {"payment_id": "synthetic-payment", "order_id": "own"}
    data = {
        "environment": "prod",
        "terminal_id": "synthetic-terminal",
        "payment_id": "synthetic-payment",
        "order_id": "own",
        "amount": "1.00",
        "payment_currency": "RUB",
        "customer_id": "9007199254740993",
        "payment_method": "xrocket",
    }
    billing._verify_payment_response(event, saved, data, strict=True)
    for delta in [
        {"test": None},
        {"test": 0},
        {"test": True},
        {"payment_method": "sbp"},
        {"terminal_id": "foreign"},
        {"amount": "2.00"},
    ]:
        with pytest.raises(DomainError):
            billing._verify_payment_response(event, saved, data | delta, strict=True)
    with pytest.raises(DomainError):
        billing._verify_payment_response(event, saved | {"scope_id": "foreign"}, data, strict=True)


def test_live_get_proves_current_saved_terminal_even_when_response_omits_terminal_field():
    from types import SimpleNamespace

    from sunrise_infrastructure.billing import Billing

    policy = SyntheticPolicy()
    policy.scope.scope_id = "synthetic-project"
    saved = {
        "order_id": "own",
        "amount": "1.23",
        "currency": "RUB",
        "account_id": "9007199254740993",
        "environment": "live",
        "provider": "rollypay",
        "terminal": "synthetic-terminal",
        "scope_id": "synthetic-project",
        "payment_method": "crypto",
    }
    data = {
        "environment": "prod",
        "payment_id": "synthetic-payment",
        "order_id": "own",
        "amount": "1.23",
        "payment_currency": "RUB",
        "customer_id": "9007199254740993",
        "payment_method": "crypto",
    }
    billing = Billing(SimpleNamespace(db=None, runtime_policy=policy), "synthetic-terminal", "live")
    billing._verify_payment_response(
        {"payment_id": "synthetic-payment", "order_id": "own"}, saved, data, strict=True
    )
    without_customer = {key: value for key, value in data.items() if key != "customer_id"}
    billing._verify_payment_response(
        {"payment_id": "synthetic-payment", "order_id": "own"}, saved, without_customer, strict=True
    )
    for delta in [{"customer_id": None}, {"customer_id": False}, {"currency": "USD"}, {"amount": "Infinity"}]:
        with pytest.raises(DomainError):
            billing._verify_payment_response(
                {"payment_id": "synthetic-payment", "order_id": "own"},
                saved,
                without_customer | delta,
                strict=True,
            )
    for terminal in [None, "foreign", False]:
        with pytest.raises(DomainError):
            billing._verify_payment_response(
                {"payment_id": "synthetic-payment", "order_id": "own"},
                saved,
                data | {"terminal_id": terminal},
                strict=True,
            )


def test_live_receipt_cannot_trigger_get_for_foreign_saved_operation_scope():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from sunrise_infrastructure.billing import Billing
    from sunrise_infrastructure.db import Inbox, Order, ProviderOperation

    policy = SyntheticPolicy()
    policy.scope.scope_id = "synthetic-project"
    inbox = Inbox(id="receipt", payload={"payment_id": "synthetic-payment", "order_id": "own"})
    order = Order(id="own", account_id=1, amount=Decimal(1), currency="RUB", environment="live")
    operation = ProviderOperation(
        id="operation",
        order_id=order.id,
        provider_id="synthetic-payment",
        terminal="synthetic-terminal",
        request={
            "order_id": "own",
            "amount": "1.00",
            "currency": "RUB",
            "account_id": "1",
            "environment": "live",
            "provider": "rollypay",
            "terminal": "synthetic-terminal",
            "scope_id": "foreign",
            "payment_method": "sbp",
        },
    )
    rows = {Inbox: inbox, Order: order}
    tx = SimpleNamespace(get=lambda model, key, **_: rows[model], scalar=lambda query: operation)

    @contextmanager
    def transaction():
        yield tx

    commerce = SimpleNamespace(db=SimpleNamespace(transaction=transaction), runtime_policy=policy)
    gets = []

    def payment(identifier):
        gets.append(identifier)
        return {
            "payment_id": identifier,
            "order_id": "own",
            "environment": "prod",
            "amount": "1.00",
            "payment_currency": "RUB",
            "customer_id": "1",
            "terminal_id": "synthetic-terminal",
            "payment_method": "sbp",
        }

    assert (
        Billing(commerce, "synthetic-terminal", "live")._reconcile_one(
            inbox.id, SimpleNamespace(payment=payment)
        )
        is False
    )
    assert inbox.state == "review" and gets == []


def test_financial_stage_revoked_while_acquiring_order_lock_denies_first_write():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from sunrise_infrastructure.billing import Billing, VerifiedPayment
    from sunrise_infrastructure.db import Account, Order, now

    policy = SyntheticPolicy()
    actor = Account(id=1, referrer_id=None)
    order = Order(id="own", account_id=1, amount=Decimal(1), currency="RUB", environment="live")
    added, fulfilled = [], []

    @contextmanager
    def nested():
        yield SimpleNamespace(rollback=lambda: None)

    def scalar(query):
        if query.column_descriptions[0].get("entity") is Order:
            policy.allowed = False  # Represents stage revocation during the real call's lock wait.
            return order
        return None

    tx = SimpleNamespace(
        get=lambda model, key, **_: order if model is Order else None,
        scalar=scalar,
        begin_nested=nested,
        add=added.append,
        flush=lambda: None,
    )

    @contextmanager
    def transaction():
        yield tx

    commerce = SimpleNamespace(
        db=SimpleNamespace(transaction=transaction),
        runtime_policy=policy,
        _account=lambda *args, **kwargs: actor,
        _lock_accounts_in=lambda session, ids: {i: i for i in ids if i is not None},
        _fulfill=lambda *args: fulfilled.append(args),
        _order_dto=lambda _: {"status": "paid"},
    )
    billing = Billing(commerce, "synthetic-terminal", "live")
    fact = VerifiedPayment(
        "rollypay", "synthetic-payment", "synthetic-terminal", "live", "own", Decimal(1), "RUB", now()
    )
    with pytest.raises(DomainError, match="недоступна"):
        billing.accept(fact)
    assert added == [] and fulfilled == []


def test_actual_fulfillment_rechecks_stage_after_subscription_lock_before_bonus_or_rights(
    quote_service, monkeypatch
):
    from sunrise_infrastructure.db import Order, Quote, Subscription, now

    service, account, _, added, _ = quote_service
    service.quote(str(account.id), "synthetic", 30, "purchase", payment_method="sbp", bonus_amount="100")
    quote = next(row for row in added if isinstance(row, Quote))
    quote.subscription_id = "existing"
    quote.snapshot = quote.snapshot | {"action": "renew", "version": 1}
    order = Order(
        id="own",
        account_id=account.id,
        amount=Decimal(900),
        currency="RUB",
        environment="live",
        status="awaiting_payment",
        fulfilled_at=None,
        created_at=now(),
    )
    sub = Subscription(
        id="existing",
        account_id=account.id,
        product_id="synthetic",
        environment="live",
        status="active",
        version=1,
        devices_limit=1,
    )
    policy = SyntheticPolicy()
    service.runtime_policy = policy

    def locked(*args):
        policy.allowed = False
        return sub

    monkeypatch.setattr(service, "_sub", locked)
    monkeypatch.setattr(service, "_provision_payload", lambda *args, **kwargs: {})
    account.bonus_reserved = Decimal(100)
    before = len(added)
    with service.db.transaction() as session:
        with pytest.raises(DomainError):
            service._fulfill(session, account, order, quote, "synthetic-payment")
    assert account.bonus_balance == Decimal(1000) and account.bonus_reserved == Decimal(100)
    assert order.fulfilled_at is None and sub.version == 1
    assert len(added) == before


def test_live_checkout_render_uses_narrowed_root_approved_host(quote_service, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from sunrise_infrastructure import commerce as module
    from sunrise_infrastructure.db import Order, now

    service, _, _, _, _ = quote_service
    policy = SyntheticPolicy()
    policy.scope.rollypay.checkout_hosts = ["approved.synthetic.test"]
    service.runtime_policy = policy
    order = Order(
        id="own",
        quote_id="quote",
        account_id=1,
        status="awaiting_payment",
        amount=Decimal(1),
        currency="RUB",
        environment="live",
        created_at=now(),
    )
    quote = SimpleNamespace(snapshot={}, expires_at=now() + timedelta(minutes=5))
    operation = SimpleNamespace(state="created", pay_url="https://pay.rollypay.io/synthetic", request={})
    session = SimpleNamespace(get=lambda *_: quote, scalar=lambda _: operation)
    monkeypatch.setattr(module, "object_session", lambda _: session)
    assert service._order_dto(order)["checkout_url"] is None
    operation.pay_url = "https://approved.synthetic.test/synthetic"
    assert service._order_dto(order)["checkout_url"] == operation.pay_url


def test_existing_live_agreement_cancellation_does_not_require_status_or_new_invoice_gate():
    policy = SyntheticPolicy()

    def require(capability=None):
        if capability in {"existing_recurring_status", "invoice_create"}:
            raise DomainError("production_stage_denied", "Операция недоступна.", 503)

    policy.require = require
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"environment": "prod", "state": "stopped", "id": "saved-agreement"})

    provider = RollyPay(
        policy.scope.rollypay.origin,
        "synthetic-key",
        policy.scope.rollypay.terminal,
        {"bank.synthetic.test"},
        environment="live",
        runtime_policy=policy,
        transport=httpx.MockTransport(handle),
    )
    assert provider.stop_agreement("saved-agreement")["state"] == "stopped"
    assert calls == [
        ("POST", "/api/v1/subscriptions/saved-agreement/stop"),
        ("GET", "/api/v1/subscriptions/saved-agreement"),
    ]
    with pytest.raises(DomainError):
        provider.agreement("saved-agreement")
    assert len(calls) == 2


def test_live_provider_numeric_json_money_is_decimal_without_binary_float_conversion():
    policy = SyntheticPolicy()
    provider = RollyPay(
        policy.scope.rollypay.origin,
        "synthetic-key",
        policy.scope.rollypay.terminal,
        {"bank.synthetic.test"},
        environment="live",
        runtime_policy=policy,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=b'{"amount":123.45,"platform_fee_usdt":0.23,"customer_id":"9007199254740993"}'
            )
        ),
    )
    result = provider.payment("synthetic-own-payment")
    assert type(result["amount"]) is Decimal and result["amount"] == Decimal("123.45")
    assert result["platform_fee_usdt"] == Decimal("0.23")
    assert result["customer_id"] == "9007199254740993"


@pytest.mark.parametrize(
    "delta",
    [
        {},
        {"amount": "2.00"},
        {"amount": True},
        {"amount": "NaN"},
        {"payment_currency": "USD"},
        {"currency": "USD"},
        {"customer_id": None},
        {"terminal_id": None},
        {"_revoke_sub": True},
    ],
)
def test_actual_live_invoice_creation_validates_immutable_money_and_optional_identity_fields(delta):
    from contextlib import contextmanager
    from datetime import timedelta
    from types import SimpleNamespace

    from sunrise_infrastructure.billing import Billing
    from sunrise_infrastructure.db import Order, ProviderOperation, Quote, now

    policy = SyntheticPolicy()
    policy.scope.scope_id = "synthetic-scope"
    policy.scope.rollypay.checkout_hosts = ["approved.synthetic.test"]
    order = Order(
        id="own",
        account_id=9007199254740993,
        quote_id="quote",
        environment="live",
        amount=Decimal(1),
        currency="RUB",
        subscription_id=None,
    )
    quote = Quote(
        id="quote",
        expires_at=now() + timedelta(minutes=1),
        snapshot={
            "payment_method": {"id": "xrocket"},
            "payment_method_policy": {"provider_method": "xrocket"},
        },
    )
    rows = []
    tx = SimpleNamespace(
        scalar=lambda query: order if query.column_descriptions[0].get("entity") is Order else None,
        get=lambda model, key, **_: (
            order
            if model is Order
            else quote
            if model is Quote
            else rows[0]
            if model is ProviderOperation
            else None
        ),
        add=rows.append,
        flush=lambda: setattr(rows[0], "id", "operation"),
    )

    @contextmanager
    def transaction():
        yield tx

    commerce = SimpleNamespace(
        db=SimpleNamespace(transaction=transaction),
        runtime_policy=policy,
        public_settings=None,
        _account=lambda *args, **kwargs: SimpleNamespace(id=order.account_id),
        _order_dto=lambda _: {"checkout_url": rows[0].pay_url},
    )
    response = {
        "order_id": "own",
        "payment_id": "saved-payment",
        "amount": "1.00",
        "payment_currency": "RUB",
        "environment": "prod",
        "payment_method": "xrocket",
        "pay_url": "https://approved.synthetic.test/checkout",
    } | delta
    provider = SimpleNamespace(terminal="synthetic-terminal", create_payment=lambda **kwargs: response)
    if delta.get("_revoke_sub"):
        order.subscription_id = "existing"
        commerce._sub = lambda *args: setattr(policy, "allowed", False)
        commerce._assert_subscription_operations = lambda *args: None
        with pytest.raises(DomainError):
            Billing(commerce, "synthetic-terminal", "live").prepare_payment("own", provider)
        assert rows == []
        return
    result = Billing(commerce, "synthetic-terminal", "live").prepare_payment("own", provider)
    if delta:
        assert result == {"state": "review", "checkout_url": None}
        assert rows[0].pay_url is None
    else:
        assert result["state"] == "created" and result["checkout_url"] == response["pay_url"]
    assert rows[0].request["account_id"] == "9007199254740993"


def test_actual_create_order_rechecks_stage_after_promo_lock_before_reserving_wallet(
    quote_service, monkeypatch
):
    from sunrise_infrastructure.db import Account, Quote, Setting

    service, account, _, added, _ = quote_service
    service.quote(
        str(account.id),
        "synthetic",
        30,
        "purchase",
        payment_method="sbp",
        bonus_amount="100",
        credit_amount="100",
    )
    quote = next(row for row in added if isinstance(row, Quote))
    quote.snapshot = quote.snapshot | {"promo_code": "synthetic", "promo_activation_id": None}
    policy = SyntheticPolicy()
    service.runtime_policy = policy
    with service.db.transaction() as session:

        def scalar(query, *args):
            entity = getattr(query, "column_descriptions", [{}])[0].get("entity")
            if entity is Setting and query._for_update_arg is not None:
                policy.allowed = False
            return account if entity is Account else quote if entity is Quote else None

        session.scalar = scalar
    monkeypatch.setattr(service, "_promo", lambda *args: {"code": "synthetic"})
    monkeypatch.setattr(service, "_order_dto", lambda _: {})
    before = len(added)
    with pytest.raises(DomainError):
        service.create_order(str(account.id), quote.id, "synthetic-order-key")
    assert account.bonus_reserved == 0 and account.credit_reserved == 0
    assert len(added) == before
