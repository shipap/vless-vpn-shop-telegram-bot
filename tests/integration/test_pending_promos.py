from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Alias, Inbox, Ledger, PromoUse, Quote, Setting

from tests.e2e.local_auth_server import SyntheticSettings
from tests.integration.test_payment_pipeline import SyntheticProvider
from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def pending_promo():
    with isolated_database("pending_promos_test") as db:
        commerce = Commerce(db, True)
        codes = ["P" + uuid4().hex[:15].upper() for _ in range(2)]
        with commerce.db.transaction() as session:
            account = Account(email=f"{uuid4()}@example.test", email_verified=True)
            session.add(account)
            session.flush()
            aid = account.id
            policy = session.get(Setting, "discounts")
            original = policy.value
            policy.value = original | {
                "secondary": [],
                "cap": "40",
                "referral_percent": "15",
                "promos": original.get("promos", [])
                + [
                    {"code": code, "percent": "20", "enabled": True, "per_account_limit": 5} for code in codes
                ],
            }
        accept_test_legal(commerce, aid)
        yield commerce, aid, codes


def test_pending_promo_persists_and_only_confirmed_benefit_consumes(pending_promo):
    commerce, aid, codes = pending_promo
    assert commerce.active_promo(aid) is None
    activated = commerce.activate_promo(aid, codes[0].lower())
    assert activated == {
        "code": codes[0],
        "percent": "20",
        "state": "pending",
        "reward_type": "percent",
        "amount_rub": None,
        "reward_days": None,
        "product_id": None,
    }
    assert Commerce(commerce.db).dashboard(aid)["active_promo"] == activated | {
        "available": True,
        "unavailable_reason": None,
    }
    assert commerce.activate_promo(aid, codes[0]) == activated
    with pytest.raises(DomainError) as error:
        commerce.activate_promo(aid, codes[1])
    assert error.value.code == "promo_pending"
    quote = commerce.quote(aid, "standard", 30, "purchase")
    assert quote["promo_code"] == codes[0] and quote["total"] == "79.20"
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    assert commerce.active_promo(aid)["code"] == activated["code"]
    commerce.test_pay(aid, order["id"])
    commerce.test_pay(aid, order["id"])
    assert commerce.active_promo(aid) is None
    assert commerce.activate_promo(aid, codes[1])["code"] == codes[1]
    with commerce.db.transaction() as session:
        assert session.get(PromoUse, order["id"]).state == "used"


def test_pending_promo_concurrent_activation_has_one_winner(pending_promo):
    commerce, aid, codes = pending_promo

    def activate(code):
        try:
            return commerce.activate_promo(aid, code)
        except DomainError as error:
            assert error.code == "promo_pending"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(activate, codes))
    assert sum(value is not None for value in results) == 1
    assert commerce.active_promo(aid)["code"] == next(value for value in results if value)["code"]


def test_pending_promo_one_order_even_when_per_code_limit_allows_more(pending_promo):
    commerce, aid, codes = pending_promo
    commerce.activate_promo(aid, codes[0])
    quotes = [commerce.quote(aid, "standard", 30, "purchase") for _ in range(2)]

    def reserve(quote):
        try:
            return commerce.create_order(aid, quote["id"], str(uuid4()))
        except DomainError as error:
            assert error.code == "promo_reserved"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, quotes))
    assert sum(value is not None for value in results) == 1


def test_pending_promo_canceled_payment_releases_only_order_reservation(pending_promo):
    commerce, aid, codes = pending_promo
    commerce.activate_promo(aid, codes[0])
    commerce.local_test = False
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    provider = SyntheticProvider()
    billing = Billing(commerce, provider.terminal, "sandbox")
    billing.prepare_payment(order["id"], provider)
    provider.payments["test-" + order["id"]]["status"] = "canceled"
    with commerce.db.transaction() as session:
        session.add(
            Inbox(
                event_key=str(uuid4()), payload={"payment_id": "test-" + order["id"], "order_id": order["id"]}
            )
        )
    billing.reconcile_inbox(provider)
    assert commerce.order(aid, order["id"])["status"] == "canceled"
    assert commerce.active_promo(aid)["code"] == codes[0]
    replacement = commerce.quote(aid, "standard", 30, "purchase")
    next_order = commerce.create_order(aid, replacement["id"], str(uuid4()))
    assert next_order["status"] == "awaiting_payment"
    billing.prepare_payment(next_order["id"], provider)
    provider.payments["test-" + next_order["id"]]["status"] = "canceled"
    with commerce.db.transaction() as session:
        session.add(
            Inbox(
                event_key=str(uuid4()),
                payload={"payment_id": "test-" + next_order["id"], "order_id": next_order["id"]},
            )
        )
    billing.reconcile_inbox(provider)
    assert commerce.order(aid, next_order["id"])["status"] == "canceled"


def test_pending_promo_weaker_than_other_discount_remains_pending(pending_promo):
    commerce, aid, codes = pending_promo
    commerce.activate_promo(aid, codes[0])
    with commerce.db.transaction() as session:
        policy = session.get(Setting, "discounts")
        policy.value = policy.value | {"secondary": ["30"]}
    quote = commerce.quote(aid, "standard", 30, "purchase")
    assert quote["promo_code"] is None and quote["total"] == "69.30"
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    commerce.test_pay(aid, order["id"])
    assert commerce.active_promo(aid)["code"] == codes[0]


def test_pending_promo_stale_activation_cannot_be_replayed(pending_promo):
    commerce, aid, codes = pending_promo
    commerce.activate_promo(aid, codes[0])
    quotes = [commerce.quote(aid, "standard", 30, "purchase") for _ in range(2)]
    order = commerce.create_order(aid, quotes[0]["id"], str(uuid4()))
    commerce.test_pay(aid, order["id"])
    commerce.activate_promo(aid, codes[1])
    with pytest.raises(DomainError) as error:
        commerce.create_order(aid, quotes[1]["id"], str(uuid4()))
    assert error.value.code == "quote_stale"


def test_pending_promo_account_uses_code_once_despite_old_larger_limit(pending_promo):
    commerce, aid, codes = pending_promo
    commerce.activate_promo(aid, codes[0])
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    commerce.test_pay(aid, order["id"])
    with pytest.raises(DomainError) as error:
        commerce.activate_promo(aid, codes[0])
    assert error.value.code == "promo_used"


def test_pending_promo_expired_paid_quote_retains_pending(pending_promo):
    from datetime import timedelta

    from sunrise_infrastructure.db import now

    commerce, aid, codes = pending_promo
    commerce.activate_promo(aid, codes[0])
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    with commerce.db.transaction() as session:
        session.get(Quote, quote["id"]).expires_at = now() - timedelta(seconds=1)
    assert commerce.test_pay(aid, order["id"])["status"] == "reconciliation_required"
    assert commerce.active_promo(aid)["code"] == codes[0]


def test_pending_promo_canonical_alias_and_referral_registration(pending_promo):
    commerce, aid, codes = pending_promo
    old_id = -int(uuid4().int % 1000000000) - 1
    with commerce.db.transaction() as session:
        session.add(Alias(old_id=old_id, canonical_id=aid))
    commerce.activate_promo(old_id, codes[0])
    assert commerce.active_promo(aid)["code"] == codes[0]
    info = commerce.referral_info(aid)
    assert info["referral_code"] == "ref_" + str(aid) and info["reward_percent"] == "15"
    telegram_id = int(uuid4().int % 1000000000000) + 100000
    invited = commerce.resolve_telegram(telegram_id, "ref_" + str(old_id))
    with commerce.db.transaction() as session:
        assert session.get(Account, int(invited["id"])).referrer_id == aid
    commerce.resolve_telegram(telegram_id, "ref_999999999999999")
    assert commerce.referral_info(aid)["invited_count"] == 1


def test_pending_promo_referral_is_new_account_only_and_actual_ledger(pending_promo):
    commerce, aid, _ = pending_promo
    telegram_id = int(uuid4().int % 1000000000000) + 100000
    account = commerce.resolve_telegram(telegram_id)
    commerce.resolve_telegram(telegram_id, "ref_" + str(aid))
    with commerce.db.transaction() as session:
        assert session.get(Account, int(account["id"])).referrer_id is None
        session.add(
            Ledger(account_id=aid, operation="referral_payment:" + str(uuid4()), amount=Decimal("12.34"))
        )
        session.add(Ledger(account_id=aid, operation="unrelated:" + str(uuid4()), amount=Decimal("98")))
    assert Decimal(commerce.referral_info(aid)["rewarded_amount"]) == Decimal("12.34")


def configure_reward(commerce, code, **values):
    with commerce.db.transaction() as session:
        policy = session.get(Setting, "discounts")
        policy.value = policy.value | {
            "promos": [promo | values if promo["code"] == code else promo for promo in policy.value["promos"]]
        }


def test_pending_promo_fixed_rub_competes_without_percentage_cap(pending_promo):
    commerce, aid, codes = pending_promo
    configure_reward(commerce, codes[0], reward_type="fixed_rub", amount_rub="30.00")
    commerce.activate_promo(aid, codes[0])
    quote = commerce.quote(aid, "standard", 30, "purchase")
    assert quote["total"] == "69.00" and quote["promo_code"] == codes[0]
    with commerce.db.transaction() as session:
        policy = session.get(Setting, "discounts")
        policy.value = policy.value | {"secondary": ["35"]}
    quote = commerce.quote(aid, "standard", 30, "purchase")
    assert quote["total"] == "64.35" and quote["promo_code"] is None
    configure_reward(commerce, codes[0], amount_rub="999.00")
    quote = commerce.quote(aid, "standard", 30, "purchase")
    assert quote["total"] == "0.00"  # This fixture has no configured minimum payment policy.


def test_fixed_money_reward_covers_renewal_devices_and_preserves_paid_basis(pending_promo):
    from sunrise_infrastructure.db import Segment, Subscription

    commerce, aid, codes = pending_promo
    purchase = commerce.quote(aid, "standard", 30, "purchase")
    original = commerce.create_order(aid, purchase["id"], str(uuid4()))
    commerce.test_pay(aid, original["id"])
    sid = commerce.order(aid, original["id"])["subscription_id"]
    devices = commerce.quote(aid, "standard", 30, "devices", sid, extra_devices=1)
    addon = commerce.create_order(aid, devices["id"], str(uuid4()))
    commerce.test_pay(aid, addon["id"])
    configure_reward(commerce, codes[0], reward_type="fixed_rub", amount_rub="2222.00")
    with commerce.db.transaction() as session:
        session.add(
            Setting(
                key="payment_methods",
                value={
                    "version": 1,
                    "minimum_subscription_amount": "1.00",
                    "methods": [
                        {
                            "id": "sbp",
                            "label": "СБП",
                            "provider": "rollypay",
                            "provider_method": "sbp",
                            "discount_percent": "0",
                            "enabled": True,
                            "currencies": ["RUB"],
                            "mapping_contract": "synthetic-reviewed-v1",
                        }
                    ],
                },
            )
        )
    commerce.activate_promo(aid, codes[0])
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    assert quote["total"] == "1.00" and quote["devices_limit"] == 5
    assert Decimal(quote["promo_discount_amount"]) == Decimal(quote["base_amount"]) - 1
    renewal = commerce.create_order(aid, quote["id"], str(uuid4()))
    commerce.test_pay(aid, renewal["id"])
    commerce.test_pay(aid, renewal["id"])
    with commerce.db.transaction() as session:
        segments = list(session.scalars(select(Segment).where(Segment.order_id == renewal["id"])))
        assert len(segments) == 1 and segments[0].paid_value == Decimal("1.00")
        assert segments[0].devices == 5
        assert session.get(Subscription, sid).devices_limit == 5
        assert session.get(PromoUse, renewal["id"]).state == "used"


def test_fixed_money_paid_tariff_change_uses_frozen_absolute_reward(pending_promo):
    from sunrise_infrastructure.db import Product, Segment

    commerce, aid, codes = pending_promo
    configure_reward(commerce, codes[0], reward_type="fixed_rub", amount_rub="30.00")
    with commerce.db.transaction() as session:
        session.get(Product, "standard").price = Decimal("99")
        session.get(Product, "universal").price = Decimal("249")
    commerce.activate_promo(aid, codes[0])
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    commerce.test_pay(aid, order["id"])
    sid = commerce.order(aid, order["id"])["subscription_id"]
    with commerce.db.transaction() as session:
        paid = session.scalar(select(Segment).where(Segment.order_id == order["id"]))
        assert paid.price_basis["products"] == {"standard": "69.00", "universal": "219.00"}
        session.get(Product, "standard").price = Decimal("999")
        session.get(Product, "universal").price = Decimal("2999")
    upgrade = commerce.quote(aid, "universal", 30, "upgrade", sid)
    # Elapsed seconds can trim a cent; editable catalog prices cannot change paid rights.
    assert Decimal("149.90") <= Decimal(upgrade["base_amount"]) <= Decimal("150.00")


def test_archived_pending_replacement_preserves_accepted_order(pending_promo):
    commerce, aid, codes = pending_promo
    commerce.activate_promo(aid, codes[0])
    old_quote = commerce.quote(aid, "standard", 30, "purchase")
    accepted = commerce.create_order(aid, old_quote["id"], str(uuid4()))
    with commerce.db.transaction() as session:
        session.add(Setting(key="promo_archive", value={codes[0]: {"terms": {"code": codes[0]}}}))
    commerce.activate_promo(aid, codes[1])
    commerce.test_pay(aid, accepted["id"])
    commerce.test_pay(aid, accepted["id"])
    assert commerce.active_promo(aid)["code"] == codes[1]
    with commerce.db.transaction() as session:
        assert session.get(PromoUse, accepted["id"]).state == "used"
        assert session.get(Quote, old_quote["id"]).snapshot["promo_code"] == codes[0]


def test_pending_promo_free_creates_gift_once_and_no_payment(pending_promo):
    from sunrise_infrastructure.db import Payment, Segment

    commerce, aid, codes = pending_promo
    configure_reward(
        commerce, codes[0], reward_type="free_days", reward_days=7, product_id="standard", per_account_limit=1
    )
    active = commerce.activate_promo(aid, codes[0])
    assert active["reward_type"] == "free_days" and active["reward_days"] == 7
    benefit = commerce.claim_promo(aid, codes[0])
    assert commerce.claim_promo(aid, codes[0]) == benefit
    assert commerce.active_promo(aid) is None
    with commerce.db.transaction() as session:
        segments = list(
            session.scalars(select(Segment).where(Segment.subscription_id == benefit["subscription_id"]))
        )
        assert len(segments) == 1 and segments[0].kind == "promo_gift"
        assert segments[0].paid_value == 0
        assert not session.scalar(select(Payment.id).where(Payment.account_id == aid))
    with pytest.raises(DomainError) as error:
        commerce.activate_promo(aid, codes[0])
    assert error.value.code == "promo_used"


def test_pending_promo_free_existing_multiple_requires_target_and_preserves_paid(pending_promo):
    from datetime import datetime, timedelta

    from sunrise_infrastructure.db import Segment, Subscription

    commerce, aid, codes = pending_promo
    for _ in range(2):
        quote = commerce.quote(aid, "universal", 30, "purchase")
        order = commerce.create_order(aid, quote["id"], str(uuid4()))
        commerce.test_pay(aid, order["id"])
    subscriptions = commerce.dashboard(aid)["subscriptions"]
    sid = subscriptions[0]["id"]
    old_expiry = datetime.fromisoformat(subscriptions[0]["expires_at"])
    with commerce.db.transaction() as session:
        sub = session.get(Subscription, sid)
        old_uuid = sub.panel_uuid
        paid = session.scalar(select(Segment).where(Segment.subscription_id == sid))
        old_paid = (paid.starts_at, paid.ends_at, paid.price_basis, paid.paid_value)
    configure_reward(commerce, codes[0], reward_type="free_days", reward_days=5, product_id="standard")
    commerce.activate_promo(aid, codes[0])
    with pytest.raises(DomainError) as error:
        commerce.claim_promo(aid, codes[0])
    assert error.value.code == "subscription_required"
    benefit = commerce.claim_promo(aid, codes[0], sid)
    assert datetime.fromisoformat(benefit["expires_at"]) == old_expiry + timedelta(days=5)
    with commerce.db.transaction() as session:
        sub = session.get(Subscription, sid)
        assert sub.product_id == "universal" and sub.panel_uuid == old_uuid
        paid = session.scalar(select(Segment).where(Segment.subscription_id == sid, Segment.kind == "paid"))
        assert (paid.starts_at, paid.ends_at, paid.price_basis, paid.paid_value) == old_paid


def test_pending_promo_orphan_panel_identity_requires_sync_without_mutation(pending_promo):
    from sunrise_infrastructure.db import Segment, Subscription

    commerce, aid, codes = pending_promo
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    commerce.test_pay(aid, order["id"])
    sid = commerce.order(aid, order["id"])["subscription_id"]
    with commerce.db.transaction() as session:
        sub = session.get(Subscription, sid)
        sub.panel_uuid = str(uuid4())
        before = (sub.version, sub.panel_uuid, sub.expires_at)
        count = len(list(session.scalars(select(Segment).where(Segment.subscription_id == sid))))
    configure_reward(commerce, codes[0], reward_type="free_days", reward_days=5, product_id="standard")
    commerce.activate_promo(aid, codes[0])
    with pytest.raises(DomainError) as failure:
        commerce.claim_promo(aid, codes[0], sid)
    assert failure.value.code == "panel_sync_required"
    with commerce.db.transaction() as session:
        sub = session.get(Subscription, sid)
        assert (sub.version, sub.panel_uuid, sub.expires_at) == before
        assert len(list(session.scalars(select(Segment).where(Segment.subscription_id == sid)))) == count


def test_pending_promo_free_claim_concurrent_global_limit(pending_promo):
    commerce, aid, codes = pending_promo
    with commerce.db.transaction() as session:
        other = Account(email=f"{uuid4()}@example.test")
        session.add(other)
        session.flush()
        other_id = other.id
    accept_test_legal(commerce, other_id)
    configure_reward(
        commerce, codes[0], reward_type="free_days", reward_days=5, product_id="standard", total_limit=1
    )
    for owner in (aid, other_id):
        commerce.activate_promo(owner, codes[0])

    def claim(owner):
        try:
            return commerce.claim_promo(owner, codes[0])
        except DomainError as error:
            assert error.code == "promo_used"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        benefits = list(pool.map(claim, (aid, other_id)))
    assert sum(benefit is not None for benefit in benefits) == 1


def test_pending_promo_disabled_retained_without_blocking_normal_purchase(pending_promo):
    commerce, aid, codes = pending_promo
    commerce.activate_promo(aid, codes[0])
    configure_reward(commerce, codes[0], enabled=False)
    assert commerce.quote(aid, "standard", 30, "purchase")["total"] == "99.00"
    assert commerce.active_promo(aid)["code"] == codes[0]


def test_pending_promo_free_legal_and_ownership_checked(pending_promo):
    commerce, aid, codes = pending_promo
    configure_reward(commerce, codes[0], reward_type="free_days", reward_days=5, product_id="standard")
    commerce.activate_promo(aid, codes[0])
    with pytest.raises(DomainError):
        commerce.claim_promo(aid, codes[0], "unknown-subscription")
    assert commerce.active_promo(aid)["code"] == codes[0]


def test_pending_promo_bot_admin_uses_distinct_trusted_policy_and_audit(pending_promo, tmp_path):
    import json

    import fakeredis
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.admin import Admin
    from sunrise_infrastructure.db import Audit
    from sunrise_infrastructure.security import RateGuard

    commerce, aid, codes = pending_promo
    telegram_id = int(uuid4().int % 1000000000000) + 100000
    settings = SyntheticSettings(
        _env_file=None,
        local_test=True,
        database_url=commerce.db.engine.url.render_as_string(hide_password=False),
        secret_key="pending-promo-isolated-secret-" * 3,
        email_allowlist=["admin@example.test"],
        admin_email_allowlist=["admin@example.test"],
        admin_telegram_allowlist=[],
        mail_dir=str(tmp_path),
    )
    guard = RateGuard(fakeredis.FakeRedis())
    accounts = Accounts(commerce.db, guard, settings)
    admin = Admin(commerce, guard, accounts=accounts)
    with commerce.db.transaction() as session:
        account = session.get(Account, aid)
        account.telegram_id = telegram_id
        account.email, account.email_verified = "admin@example.test", False
        assert not accounts.is_bot_admin(account)
        assert not accounts.is_admin(account)
    with pytest.raises(DomainError) as denied:
        admin.bot_promos(aid)
    assert denied.value.status == 404
    challenge = accounts.request_code("admin@example.test", "127.0.0.1")
    code = json.loads((tmp_path / (challenge["challenge_id"] + ".json")).read_text())["code"]
    accounts.verify_code(
        challenge["challenge_id"],
        code,
        "127.0.0.1",
        legal_accepted=True,
        documents=commerce.legal.public()["documents"],
    )
    with commerce.db.transaction() as session:
        assert not accounts.is_bot_admin(session.get(Account, aid))
        assert not accounts.is_admin(session.get(Account, aid))
    settings.admin_telegram_allowlist = [str(telegram_id)]
    with commerce.db.transaction() as session:
        assert accounts.is_bot_admin(session.get(Account, aid))
        assert accounts.is_admin(session.get(Account, aid))
    saved = admin.save_bot_promo(
        aid,
        {"code": codes[1], "reward_type": "fixed_rub", "amount_rub": "12.00", "total_limit": None},
        "Синтетическое создание",
    )
    assert saved["per_account_limit"] == 1 and saved["total_limit"] is None
    assert (
        next(promo for promo in admin.bot_promos(aid) if promo["code"] == codes[1])["amount_rub"] == "12.00"
    )
    with commerce.db.transaction() as session:
        assert session.scalar(select(Audit).where(Audit.actor_id == aid, Audit.action == "bot_promo_save"))
    commerce.activate_promo(aid, codes[1])
    row = next(promo for promo in admin.bot_promos(aid) if promo["code"] == codes[1])
    assert row["activated_users"] == 1 and row["redeemed_count"] == 0
    assert row["reserved_count"] == 0
    assert row["remaining_uses"] is None
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], "admin-reservation-" + uuid4().hex)
    row = next(promo for promo in admin.bot_promos(aid) if promo["code"] == codes[1])
    assert row["reserved_count"] == 1 and row["redeemed_count"] == 0
    assert row["activated_users"] == 1
    deleted = admin.delete_bot_promo(aid, codes[1], "Подтверждено удаление в тесте")
    assert deleted["deleted"] is True
    assert not any(promo["code"] == codes[1] for promo in admin.bot_promos(aid))
    with commerce.db.transaction() as session:
        assert session.get(PromoUse, order["id"]).state == "reserved"
    assert commerce.quote(aid, "standard", 30, "purchase")["promo_code"] is None
    assert commerce.activate_promo(aid, codes[0])["code"] == codes[0]
    with pytest.raises(DomainError) as archived:
        admin.save_bot_promo(aid, saved, "Попытка повторного создания")
    assert archived.value.code == "promo_archived"
    settings.admin_telegram_allowlist = []
    with pytest.raises(DomainError) as error:
        admin.bot_promos(aid)
    assert error.value.status == 404


def test_pending_promo_admin_reward_validation(pending_promo):
    import fakeredis
    from sunrise_infrastructure.admin import Admin
    from sunrise_infrastructure.security import RateGuard

    commerce, aid, codes = pending_promo
    admin = Admin(commerce, RateGuard(fakeredis.FakeRedis()))
    with pytest.raises(DomainError):
        admin.save_discounts(
            aid,
            {"promos": [{"code": codes[0], "reward_type": "fixed_rub", "amount_rub": "-1.00"}]},
            "Некорректная скидка",
        )
    with pytest.raises(DomainError):
        admin.save_discounts(
            aid,
            {"promos": [{"code": codes[0], "reward_type": "free_days", "reward_days": 5}]},
            "Нет тарифа подарка",
        )


def test_pending_promo_accepted_order_preserves_price_when_catalog_disables_code(pending_promo):
    commerce, aid, codes = pending_promo
    commerce.activate_promo(aid, codes[0])
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    configure_reward(commerce, codes[0], enabled=False, percent="0")
    assert commerce.test_pay(aid, order["id"])["status"] == "paid"
    assert commerce.active_promo(aid) is None


def test_pending_promo_theme_accepts_200_semantic_icons(pending_promo):
    import fakeredis
    from sunrise_infrastructure.admin import Admin
    from sunrise_infrastructure.security import RateGuard

    commerce, aid, _ = pending_promo
    theme = {
        "icons": {
            "icon_" + str(i): {"custom_emoji_id": str(5000000000000000000 + i), "fallback": "⭐"}
            for i in range(200)
        },
        "styles": {},
    }
    admin = Admin(commerce, RateGuard(fakeredis.FakeRedis()))
    assert len(admin.update_telegram_theme(aid, theme, "Синтетические значки")["icons"]) == 200


def test_pending_promo_renewal_gift_cannot_create_new_subscription(pending_promo):
    commerce, aid, codes = pending_promo
    configure_reward(
        commerce, codes[0], reward_type="free_days", reward_days=5, product_id="standard", audience="renewal"
    )
    commerce.activate_promo(aid, codes[0])
    with pytest.raises(DomainError) as error:
        commerce.claim_promo(aid, codes[0])
    assert error.value.code == "promo_audience"
    assert not commerce.dashboard(aid)["subscriptions"]
    assert commerce.active_promo(aid)["code"] == codes[0]


def test_pending_promo_first_purchase_audience_survives_canonical_merge(pending_promo):
    from types import SimpleNamespace

    import fakeredis
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.db import now
    from sunrise_infrastructure.security import RateGuard

    commerce, aid, codes = pending_promo
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    commerce.test_pay(aid, order["id"])
    configure_reward(commerce, codes[0], audience="first_purchase", per_account_limit=1)
    telegram = int(uuid4().int % 1000000000000) + 100000
    other = commerce.resolve_telegram(telegram)
    accept_test_legal(commerce, other["id"])
    commerce.activate_promo(other["id"], codes[0])
    quote = commerce.quote(other["id"], "standard", 30, "purchase")
    pending_order = commerce.create_order(other["id"], quote["id"], str(uuid4()))
    accounts = Accounts(
        commerce.db,
        RateGuard(fakeredis.FakeRedis()),
        SimpleNamespace(owner_id=None, admin_email_allowlist=[]),
    )
    proof = accounts.request_link(aid, str(telegram), now())["proof"]
    accounts.confirm_telegram_link(other["id"], proof)
    assert commerce.test_pay(aid, pending_order["id"])["status"] == "reconciliation_required"
    assert commerce.active_promo(aid)["code"] == codes[0]


def test_pending_promo_referral_link_uses_only_safe_configured_bot_url(pending_promo):
    commerce, aid, _ = pending_promo
    commerce.referral_bot_url = "https://t.me/example_test_bot"
    assert commerce.referral_info(aid)["referral_url"] == f"https://t.me/example_test_bot?start=ref_{aid}"
    for unsafe in (
        "https://evil.example/test",
        "https://token:secret@t.me/example_test_bot",
        "http://t.me/example_test_bot",
        "https://t.me/example_test_bot?token=secret",
    ):
        commerce.referral_bot_url = unsafe
        assert commerce.referral_info(aid)["referral_url"] is None
