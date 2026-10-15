"""Real SQL entitlement redemption with synthetic authority, never production admission."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing, VerifiedPayment
from sunrise_infrastructure.db import (
    Account,
    Alias,
    Inbox,
    Order,
    Payment,
    Product,
    PromoUse,
    ProviderOperation,
    Quote,
    Segment,
    Setting,
    Subscription,
    now,
)
from sunrise_infrastructure.promos import validate_promo

from tests.contract.test_legacy_promo_usage import CODE, GID, legacy_marker
from tests.integration.test_production_business_races import live_business as live_business
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def legacy_business(live_business):
    db, commerce, aid, sid, _, initial_iid, _ = live_business
    marker = legacy_marker(commerce.runtime_policy.scope.scope_id)
    marker["codes"][CODE]["used_account_ids"] = [str(aid)]
    marker["pending_grants"][GID]["source_account_id"] = str(aid)
    with db.transaction() as session:
        actor = session.get(Account, aid)
        actor.pending_promo = {
            "id": GID,
            "legacy_grant_id": GID,
            "code": CODE,
            "percent": "20",
            "state": "pending",
            "reward_type": "percent",
        }
        actor.credit_balance = Decimal(1000)
        actor.credit_currency = "RUB"
        product = session.get(Product, "standard")
        product.price = Decimal(100)
        product.extra_device_price = Decimal(50)
        session.get(Subscription, sid).devices_limit = 6
        session.get(Inbox, initial_iid).state = "processed"
        discounts = session.get(Setting, "discounts")
        discounts.value = discounts.value | {
            "cap": "70",
            "secondary": [],
            "promos": [validate_promo({"code": CODE, "percent": "5", "total_limit": 20})],
        }
        session.get(Setting, "legacy_promo_usage").value = marker
        session.add(
            Setting(
                key="payment_methods",
                value={
                    "version": 1,
                    "minimum_subscription_amount": "1.00",
                    "methods": [
                        {
                            "id": "sbp",
                            "label": "SBP",
                            "provider": "rollypay",
                            "provider_method": "sbp",
                            "discount_percent": "0",
                            "enabled": True,
                            "currencies": ["RUB"],
                            "mapping_contract": "synthetic-reviewed-contract",
                        }
                    ],
                },
            )
        )
    yield db, commerce, aid, sid


def own_fact(order, provider_id="synthetic-legacy-paid"):
    return VerifiedPayment(
        provider="rollypay",
        provider_id=provider_id,
        terminal="synthetic-terminal",
        environment="live",
        order_id=order["id"],
        amount=Decimal(order["total"]),
        currency="RUB",
        paid_at=now(),
    )


def own_receipt(db, commerce, aid, order, provider_id, *, create_receipt=True):
    request = {
        "scope_id": commerce.runtime_policy.scope.scope_id,
        "provider": "rollypay",
        "terminal": "synthetic-terminal",
        "environment": "live",
        "order_id": order["id"],
        "account_id": str(aid),
        "amount": order["total"],
        "currency": "RUB",
        "payment_method": "sbp",
    }
    with db.transaction() as session:
        session.add(
            ProviderOperation(
                order_id=order["id"],
                provider_id=provider_id,
                terminal="synthetic-terminal",
                state="created",
                request=request,
            )
        )
        receipt = None
        if create_receipt:
            receipt = Inbox(
                event_key="legacy:" + provider_id,
                state="pending",
                payload={"payment_id": provider_id, "order_id": order["id"]},
            )
            session.add(receipt)
        session.flush()
        iid = receipt.id if receipt else None
    fact = {
        "environment": "prod",
        "payment_id": provider_id,
        "order_id": order["id"],
        "amount": order["total"],
        "payment_currency": "RUB",
        "payment_method": "sbp",
        "status": "paid",
        "paid_at": now().isoformat(),
    }
    return iid, fact


def test_preclaimed_grant_discounts_whole_service_and_first_payment_consumes_exactly_once(legacy_business):
    db, commerce, aid, sid = legacy_business
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    assert quote["base_amount"] == "200.00"
    assert quote["total"] == "160.00"
    assert quote["promo_code"] == CODE and "legacy_promo_grant" not in quote
    with db.transaction() as session:
        saved = session.get(Quote, quote["id"])
        assert saved.snapshot["basis"]["products"]["standard"] == "160.00"
        assert saved.snapshot["device_pricing"]["amount"] == "100.00"
        assert saved.snapshot["legacy_promo_grant"]["grant_id"] == GID
    order = commerce.create_order(aid, quote["id"], "first-real-legacy-order")
    billing = Billing(commerce, "synthetic-terminal", "live")
    result = billing.accept(own_fact(order))
    assert result["status"] != "reconciliation_required"
    with db.transaction() as session:
        paid = session.get(Order, order["id"])
        assert paid.fulfilled_at is not None
        segment = session.scalar(select(Segment).where(Segment.order_id == order["id"]))
        assert segment.paid_value == Decimal(160) and segment.price_basis["products"]["standard"] == "160.00"
        assert session.get(PromoUse, order["id"]).state == "used"
        assert session.get(Account, aid).pending_promo is None
        assert len(list(session.scalars(select(Payment.id).where(Payment.order_id == order["id"])))) == 1
    assert billing.accept(own_fact(order))["id"] == order["id"]
    with db.transaction() as session:
        assert len(list(session.scalars(select(Segment.id).where(Segment.order_id == order["id"])))) == 1


@pytest.mark.parametrize(
    "cap,period_discount,expected,promo_selected",
    [("10", "0", "180.00", True), ("20", "20", "180.00", False)],
)
def test_legacy_device_factor_respects_cap_and_actual_incremental_benefit_without_method(
    legacy_business, cap, period_discount, expected, promo_selected
):
    db, commerce, aid, sid = legacy_business
    with db.transaction() as session:
        policy = session.get(Setting, "discounts")
        policy.value = policy.value | {"cap": cap}
        periods = session.get(Setting, "periods")
        periods.value = [{"days": 30, "months": 1, "discount_percent": period_discount}]
    quote = commerce.quote(aid, "standard", 30, "renew", sid, credit_amount="1000")
    assert quote["total"] == "0.00" and quote["credit_amount"] == expected
    assert (quote["promo_code"] == CODE) is promo_selected
    with db.transaction() as session:
        saved = session.get(Quote, quote["id"])
        assert saved.snapshot["basis"]["products"]["standard"] == expected
        assert ("legacy_promo_grant" in saved.snapshot) is promo_selected
        assert session.get(Account, aid).pending_promo["id"] == GID


def test_legacy_whole_service_cap_is_enforced_after_component_rounding_without_gateway(legacy_business):
    db, commerce, aid, sid = legacy_business
    with db.transaction() as session:
        product = session.get(Product, "standard")
        product.price = Decimal("100.03")
        product.extra_device_price = Decimal("100.03")
        session.get(Subscription, sid).devices_limit = 5
        discounts = session.get(Setting, "discounts")
        discounts.value = discounts.value | {"cap": "20"}
    quote = commerce.quote(aid, "standard", 30, "renew", sid, credit_amount="1000")
    assert quote["base_amount"] == "200.06"
    assert quote["credit_amount"] == "160.05" and quote["total"] == "0.00"
    with db.transaction() as session:
        assert session.get(Quote, quote["id"]).snapshot["basis"]["products"]["standard"] == "160.05"


def test_stronger_campaign_does_not_apply_device_exception_or_consume_legacy_grant(legacy_business):
    db, commerce, aid, sid = legacy_business
    with db.transaction() as session:
        policy = session.get(Setting, "discounts")
        policy.value = policy.value | {"secondary": ["30"]}
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    assert quote["total"] == "170.00" and quote["promo_code"] is None
    with db.transaction() as session:
        saved = session.get(Quote, quote["id"])
        assert "legacy_promo_grant" not in saved.snapshot
        assert saved.snapshot["basis"]["products"]["standard"] == "170.00"
    order = commerce.create_order(aid, quote["id"], "stronger-campaign")
    Billing(commerce, "synthetic-terminal", "live").accept(own_fact(order))
    with db.transaction() as session:
        assert session.get(Order, order["id"]).fulfilled_at is not None
        assert session.get(PromoUse, order["id"]) is None
        assert session.get(Account, aid).pending_promo["id"] == GID


def test_paid_grant_proof_revocation_preserves_payment_and_same_order_recovers_once(
    legacy_business, monkeypatch
):
    import sunrise_infrastructure.billing as billing_module

    db, commerce, aid, sid = legacy_business
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    order = commerce.create_order(aid, quote["id"], "proof-recovery")
    iid, fact = own_receipt(db, commerce, aid, order, "proof-recovery-payment")
    with db.transaction() as session:
        root = session.get(Setting, "legacy_promo_usage")
        saved = root.value
        root.value = saved | {"pending_grants": {}}
        original_expiry = session.get(Subscription, sid).expires_at
    billing = Billing(commerce, "synthetic-terminal", "live")
    calls = []

    def payment(identifier):
        calls.append(identifier)
        return dict(fact)

    provider = SimpleNamespace(payment=payment)
    clock = now()
    monkeypatch.setattr(billing_module, "now", lambda: clock)
    assert billing.reconcile_inbox(provider) == 1
    with db.transaction() as session:
        assert session.get(Order, order["id"]).status == "reconciliation_required"
        assert session.get(Order, order["id"]).fulfilled_at is None
        assert session.scalar(select(Payment).where(Payment.order_id == order["id"])) is not None
        assert session.scalar(select(Segment).where(Segment.order_id == order["id"])) is None
        assert session.get(Subscription, sid).expires_at == original_expiry
        assert session.get(PromoUse, order["id"]).state == "reserved"
        assert session.get(Account, aid).pending_promo["id"] == GID
        assert session.get(Inbox, iid).state == "pending"
        deadline = session.get(Inbox, iid).available_at
        payment_id = session.scalar(select(Payment.id).where(Payment.order_id == order["id"]))
    assert billing.reconcile_inbox(provider) == 0 and len(calls) == 1
    clock = deadline
    assert billing.reconcile_inbox(provider) == 1
    with db.transaction() as session:
        assert session.get(Inbox, iid).state == "pending"
        assert session.get(Order, order["id"]).fulfilled_at is None
        assert session.scalar(select(Payment.id).where(Payment.order_id == order["id"])) == payment_id
        deadline = session.get(Inbox, iid).available_at
        session.get(Setting, "legacy_promo_usage").value = saved
        # Another order must not take a grant quarantined by an accepted payment.
    quarantined = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    assert quarantined["promo_code"] is None and quarantined["total"] == "200.00"
    with db.transaction() as session:
        assert "legacy_promo_grant" not in session.get(Quote, quarantined["id"]).snapshot
        gates = session.get(Setting, "production_runtime_gates")
        gates.value = gates.value | {"financial_apply": False}
    clock = deadline
    assert billing.reconcile_inbox(provider) == 0
    with db.transaction() as session:
        assert session.get(Order, order["id"]).fulfilled_at is None
        assert session.scalar(select(Payment.id).where(Payment.order_id == order["id"])) == payment_id
        assert session.get(Inbox, iid).state == "pending"
        deadline = session.get(Inbox, iid).available_at
        gates = session.get(Setting, "production_runtime_gates")
        gates.value = gates.value | {"financial_apply": True}
    clock = deadline
    assert billing.reconcile_inbox(provider) == 1
    with db.transaction() as session:
        assert session.get(Order, order["id"]).fulfilled_at is not None
        assert session.get(PromoUse, order["id"]).state == "used"
        assert session.get(Account, aid).pending_promo is None
        assert len(list(session.scalars(select(Payment.id).where(Payment.order_id == order["id"])))) == 1
        assert len(list(session.scalars(select(Segment.id).where(Segment.order_id == order["id"])))) == 1
        assert session.get(Inbox, iid).state == "processed"
        assert session.scalar(select(Payment.id).where(Payment.order_id == order["id"])) == payment_id
    assert billing.reconcile_inbox(provider) == 0 and len(calls) == 4


def test_exact_signed_callback_handler_duplicate_preserves_one_retrying_inbox(legacy_business):
    """Actual callback body/signature/SQL with a synthetic policy, not full admission."""
    import ast
    import hashlib
    import hmac
    import inspect
    import json
    import time

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sunrise_api import main as api_module

    db, commerce, aid, sid = legacy_business
    commerce.runtime_policy.scope.rollypay.terminal = "synthetic-terminal"
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    order = commerce.create_order(aid, quote["id"], "signed-grant-intake")
    _, fact = own_receipt(db, commerce, aid, order, "signed-grant-payment", create_receipt=False)
    # Mount the exact production route handler while retaining synthetic admission.
    # No production RuntimePolicy/physical DB allowlist is replaced or broadened.
    tree = ast.parse(inspect.getsource(api_module.create_app))
    handler = next(
        node
        for node in tree.body[0].body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "rollypay"
    )
    handler.decorator_list = []
    secret = "synthetic-callback-signing-secret"  # noqa: S105 -- synthetic HMAC fixture only
    namespace = dict(api_module.create_app.__globals__) | {
        "db": db,
        "policy": commerce.runtime_policy,
        "config": SimpleNamespace(rollypay_signing_secret=secret),
    }
    exec(  # noqa: S102 -- exact repository route AST, no data-provided code
        compile(
            ast.Module(body=[handler], type_ignores=[]), inspect.getsourcefile(api_module.create_app), "exec"
        ),
        namespace,
    )
    app = FastAPI()
    app.add_api_route("/api/v1/webhooks/rollypay", namespace["rollypay"], methods=["POST"])
    raw = json.dumps(
        {
            "payment_id": fact["payment_id"],
            "order_id": order["id"],
            "status": "paid",
            "amount": fact["amount"],
            "currency": "RUB",
        }
    ).encode()
    timestamp = str(int(time.time()))
    headers = {
        "x-timestamp": timestamp,
        "x-signature": hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest(),
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/webhooks/rollypay", content=raw, headers=headers).status_code == 200
        with db.transaction() as session:
            row = session.scalar(select(Inbox).where(Inbox.event_key == hashlib.sha256(raw).hexdigest()))
            iid = row.id
            assert row.payload["receipt"]["signed_body"] == raw.decode()
            assert row.payload["receipt"]["scope_id"] == commerce.runtime_policy.scope.scope_id
            marker = session.get(Setting, "legacy_promo_usage")
            saved = marker.value
            marker.value = saved | {"pending_grants": {}}
        billing = Billing(commerce, "synthetic-terminal", "live")
        assert billing._reconcile_one(iid, SimpleNamespace(payment=lambda identifier: dict(fact)))
        assert client.post("/api/v1/webhooks/rollypay", content=raw, headers=headers).status_code == 200
        with db.transaction() as session:
            rows = list(
                session.scalars(select(Inbox).where(Inbox.event_key == hashlib.sha256(raw).hexdigest()))
            )
            assert len(rows) == 1 and rows[0].id == iid and rows[0].state == "pending"
            assert session.get(Order, order["id"]).fulfilled_at is None
            assert len(list(session.scalars(select(Payment.id).where(Payment.order_id == order["id"])))) == 1


@pytest.mark.parametrize("current_state", ["reserved", "used"])
def test_cancellation_releases_same_grant_but_late_old_payment_cannot_issue_second_benefit(
    legacy_business, current_state
):
    db, commerce, aid, sid = legacy_business
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    old = commerce.create_order(aid, quote["id"], "cancelled-preclaimed")
    iid, fact = own_receipt(db, commerce, aid, old, "cancelled-preclaimed-payment")
    billing = Billing(commerce, "synthetic-terminal", "live")
    assert billing._reconcile_one(
        iid, SimpleNamespace(payment=lambda identifier: fact | {"status": "canceled"})
    )
    with db.transaction() as session:
        assert session.get(Order, old["id"]).status == "canceled"
        assert session.get(PromoUse, old["id"]).state == "released"
    fresh = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    current = commerce.create_order(aid, fresh["id"], "resumed-preclaimed")
    if current_state == "used":
        Billing(commerce, "synthetic-terminal", "live").accept(
            own_fact(current, "resumed-preclaimed-payment")
        )
    assert billing._reconcile_one(iid, SimpleNamespace(payment=lambda identifier: dict(fact)))
    with db.transaction() as session:
        assert session.get(Order, old["id"]).status == "reconciliation_required"
        assert session.get(Order, old["id"]).fulfilled_at is None
        assert session.get(PromoUse, old["id"]).state == "released"
        assert session.get(PromoUse, current["id"]).state == current_state
        assert session.scalar(select(Segment).where(Segment.order_id == old["id"])) is None
        assert (session.scalar(select(Segment).where(Segment.order_id == current["id"])) is not None) is (
            current_state == "used"
        )
        assert session.scalar(select(Payment).where(Payment.order_id == old["id"])) is not None


def test_signed_negative_bonus_does_not_block_external_renewal_or_forgive_debt(legacy_business):
    db, commerce, aid, sid = legacy_business
    debt = Decimal("-0.000001")
    with db.transaction() as session:
        actor = session.get(Account, aid)
        actor.bonus_balance = debt
        reserved = actor.bonus_reserved
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    assert quote["bonus_amount"] == "0.00" and quote["total"] == "160.00"
    order = commerce.create_order(aid, quote["id"], "negative-bonus-fully-external")
    Billing(commerce, "synthetic-terminal", "live").accept(own_fact(order))
    with db.transaction() as session:
        actor = session.get(Account, aid)
        assert actor.bonus_balance == debt and actor.bonus_reserved == reserved
        assert session.get(Order, order["id"]).fulfilled_at is not None


def test_signed_negative_bonus_cannot_redeem_positive_bonus_or_accept_negative_client_request(
    legacy_business,
):
    db, commerce, aid, sid = legacy_business
    with db.transaction() as session:
        session.get(Account, aid).bonus_balance = Decimal("-0.000001")
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp", bonus_amount="1")
    with pytest.raises(DomainError) as denied:
        commerce.create_order(aid, quote["id"], "negative-bonus-cannot-redeem")
    assert denied.value.code == "bonus_insufficient"
    with pytest.raises(ValueError):
        commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp", bonus_amount="-1")


def test_root_grant_follows_proven_alias_without_source_account_or_double_global_count(legacy_business):
    db, commerce, aid, sid = legacy_business
    source = aid + 100000
    with db.transaction() as session:
        assert session.get(Account, source) is None
        session.add(Alias(old_id=source, canonical_id=aid))
        marker = session.get(Setting, "legacy_promo_usage")
        value = legacy_marker(commerce.runtime_policy.scope.scope_id)
        value["codes"][CODE]["used_account_ids"] = [str(source)]
        value["pending_grants"][GID]["source_account_id"] = str(source)
        marker.value = value
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    order = commerce.create_order(aid, quote["id"], "alias-root-grant")
    assert (
        Billing(commerce, "synthetic-terminal", "live").accept(own_fact(order))["status"]
        != "reconciliation_required"
    )
    with db.transaction() as session:
        assert session.get(PromoUse, order["id"]).state == "used"
        assert session.get(Quote, quote["id"]).snapshot["legacy_promo_grant"]["source_account_id"] == str(
            source
        )
        assert session.get(Setting, "legacy_promo_usage").value == value


def test_new_claims_count_root_baseline_plus_real_modern_reservation(legacy_business):
    (
        db,
        commerce,
        _,
        _,
    ) = legacy_business
    with db.transaction() as session:
        marker = session.get(Setting, "legacy_promo_usage")
        marker.value = marker.value | {
            "codes": marker.value["codes"]
            | {"OFFSET": {"global_claimed": 1, "source_counter": 1, "used_account_ids": ["9007199254740997"]}}
        }
        policy = session.get(Setting, "discounts")
        policy.value = policy.value | {
            "promos": policy.value["promos"]
            + [validate_promo({"code": "OFFSET", "percent": "10", "total_limit": 2})]
        }
        first, second = Account(), Account()
        session.add_all([first, second])
        session.flush()
        ids = first.id, second.id
    for aid in ids:
        accept_test_legal(commerce, aid)
    quote = commerce.quote(ids[0], "standard", 30, "purchase", promo_code="OFFSET", payment_method="sbp")
    order = commerce.create_order(ids[0], quote["id"], "last-global-slot")
    with pytest.raises(DomainError) as exhausted:
        commerce.quote(ids[1], "standard", 30, "purchase", promo_code="OFFSET", payment_method="sbp")
    assert exhausted.value.code == "promo_used"
    with db.transaction() as session:
        assert session.get(PromoUse, order["id"]).state == "reserved"
        assert session.get(Setting, "legacy_promo_usage").value["codes"]["OFFSET"]["global_claimed"] == 1


def test_concurrent_order_creation_reserves_one_root_grant(legacy_business):
    db, commerce, aid, sid = legacy_business
    quotes = [commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp") for _ in range(2)]
    barrier = Barrier(2)

    def create(index):
        barrier.wait(timeout=5)
        try:
            return commerce.create_order(aid, quotes[index]["id"], "concurrent-grant-" + str(index))
        except DomainError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, DomainError) and result.code == "promo_reserved" for result in results) == 1
    with db.transaction() as session:
        uses = list(session.scalars(select(PromoUse).where(PromoUse.code == CODE)))
        assert len(uses) == 1 and uses[0].state == "reserved"


@pytest.mark.parametrize(
    "change", ["scope", "refund", "canceled", "used", "multiple", "expired", "unrelated", "released"]
)
def test_automatic_recovery_refuses_other_financial_or_grant_states(legacy_business, change, monkeypatch):
    import sunrise_infrastructure.billing as billing_module

    db, commerce, aid, sid = legacy_business
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    order = commerce.create_order(aid, quote["id"], "negative-recovery-" + change)
    iid, fact = own_receipt(db, commerce, aid, order, "negative-recovery-payment")
    billing = Billing(commerce, "synthetic-terminal", "live")
    with db.transaction() as session:
        marker = session.get(Setting, "legacy_promo_usage")
        saved = marker.value
        marker.value = saved | {"pending_grants": {}}
    provider = SimpleNamespace(payment=lambda identifier: dict(fact))
    assert billing.reconcile_inbox(provider) == 1
    with db.transaction() as session:
        session.get(Setting, "legacy_promo_usage").value = saved
        row = session.get(Order, order["id"])
        captured = session.get(Quote, quote["id"])
        deadline = session.get(Inbox, iid).available_at
        if change == "scope":
            captured.snapshot = captured.snapshot | {
                "legacy_promo_grant": captured.snapshot["legacy_promo_grant"] | {"scope_id": "foreign"}
            }
        elif change == "canceled":
            row.status = "canceled"
        elif change in {"used", "released"}:
            session.get(PromoUse, order["id"]).state = change
        elif change == "expired":
            captured.expires_at = now() - timedelta(seconds=1)
        elif change == "unrelated":
            captured.snapshot = {
                key: value for key, value in captured.snapshot.items() if key != "legacy_promo_grant"
            }
    if change == "refund":
        billing.refund(fact["payment_id"], "synthetic-full-refund", Decimal(fact["amount"]), now())
    elif change == "multiple":
        billing.accept(own_fact(order, "second-accepted-payment"))
    monkeypatch.setattr(billing_module, "now", lambda: deadline)
    assert billing.reconcile_inbox(provider) == 1
    with db.transaction() as session:
        assert session.get(Order, order["id"]).fulfilled_at is None
        assert session.scalar(select(Segment).where(Segment.order_id == order["id"])) is None
        assert session.get(Inbox, iid).state == "processed"
        assert len(list(session.scalars(select(Payment.id).where(Payment.order_id == order["id"])))) == (
            2 if change == "multiple" else 1
        )
