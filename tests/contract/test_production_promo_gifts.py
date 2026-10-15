from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Job,
    Payment,
    Product,
    PromoClaim,
    Segment,
    Setting,
    Subscription,
)
from sunrise_infrastructure.promos import Promos


@pytest.fixture
def gift_service(monkeypatch):
    account = Account(
        id=9007199254740993, pending_promo={"id": "genuine-claim", "code": "GIFT"}, legacy_consent={}
    )
    product = Product(
        id="gift-product",
        available=True,
        included_devices=4,
        currency="RUB",
        internal_squad_uuid="11111111-1111-4111-8111-111111111111",
    )
    added = []
    promo = {
        "code": "GIFT",
        "reward_type": "free_days",
        "reward_days": 3,
        "product_id": product.id,
        "trial": False,
    }

    def scalar(query, *args):
        entity = getattr(query, "column_descriptions", [{}])[0].get("entity")
        if entity is Account:
            return account
        if entity is PromoClaim:
            code = query.compile().params.get("code_1")
            return next((row for row in added if isinstance(row, PromoClaim) and row.code == code), None)
        return None

    def flush():
        for row in added:
            if hasattr(row, "id") and row.id is None:
                row.id = "own-" + type(row).__name__
            if isinstance(row, Subscription):
                row.version = row.version or 1
                row.status = row.status or "active"

    tx = SimpleNamespace(
        scalar=scalar,
        get=lambda model, key, **_: (
            product
            if model is Product
            else next(
                (row for row in added if isinstance(row, Setting) and model is Setting and row.key == key),
                None,
            )
        ),
        scalars=lambda query: [],
        add=added.append,
        flush=flush,
        execute=lambda *args: None,
    )

    @contextmanager
    def transaction():
        yield tx

    service = Commerce(SimpleNamespace(transaction=transaction), local_test=False)
    service.runtime_policy = SimpleNamespace(
        production=True,
        scope=SimpleNamespace(scope_id="synthetic-project"),
        require_current=lambda *args: None,
    )
    monkeypatch.setattr(service, "_refresh_panel", lambda *args: {})
    monkeypatch.setattr(service, "_promo", lambda *args: promo)
    monkeypatch.setattr(service.legal.__class__, "require_current", lambda *args: None)
    monkeypatch.setattr(
        service,
        "_provision_payload",
        lambda session, sub, **kwargs: {
            "subscription_id": sub.id,
            "environment": sub.environment,
            "scope_id": "synthetic-project",
        },
    )
    return service, account, promo, added, tx


def test_actual_daygift_commits_live_claim_and_finite_gift_without_fabricating_payment(gift_service):
    service, account, _, added, _ = gift_service
    result = Promos(service).claim(str(account.id), "GIFT")
    sub = next(row for row in added if isinstance(row, Subscription))
    claim = next(row for row in added if isinstance(row, PromoClaim))
    segment = next(row for row in added if isinstance(row, Segment))
    job = next(row for row in added if isinstance(row, Job))
    assert sub.environment == "live" and sub.devices_limit == segment.devices == 4
    assert segment.kind == "promo_gift" and segment.paid_value == Decimal(0)
    assert claim.account_id == account.id and claim.segment_id == segment.id and claim.result == result
    assert job.payload["promo_claim_id"] == claim.id
    assert job.payload["gift_segment_id"] == segment.id
    assert all(not isinstance(row, Payment) for row in added)
    assert Promos(service).claim(str(account.id), "GIFT") == result
    assert len([row for row in added if isinstance(row, PromoClaim)]) == 1


@pytest.mark.parametrize("legacy", [None, 1, True, "0"])
def test_trial_unknown_or_spent_imported_history_cannot_be_reset(gift_service, legacy):
    service, account, promo, added, _ = gift_service
    promo["trial"] = True
    account.legacy_consent = {
        "legacy_free_trial_used": legacy,
        "production_access": {
            "version": 1,
            "account_id": str(account.id),
            "state": "active",
            "source": "legacy_user_is_active",
            "source_account_id": str(account.id),
        },
    }
    with pytest.raises(DomainError):
        Promos(service).claim(str(account.id), "GIFT")
    assert added == []


def test_actual_provision_binding_accepts_only_own_durable_consumed_gift_claim(gift_service):
    from sunrise_infrastructure.provisioning import Provisioning

    service, account, _, added, tx = gift_service
    Promos(service).claim(str(account.id), "GIFT")
    sub = next(row for row in added if isinstance(row, Subscription))
    job = next(row for row in added if isinstance(row, Job))
    original_get = tx.get
    tx.get = lambda model, key, **kwargs: (
        account
        if model is Account and key == account.id
        else next((row for row in added if type(row) is model and getattr(row, "id", None) == key), None)
        or original_get(model, key, **kwargs)
    )
    registered = []
    panel = SimpleNamespace(
        runtime_policy=service.runtime_policy,
        api_host="panel.synthetic.test",
        register_project_binding=lambda binding, **kwargs: registered.append(binding),
    )
    provisioning = Provisioning(service.db, panel, commerce=service)
    account.telegram_id = 12345
    binding = provisioning._project_binding(tx, job, sub, job.payload)
    assert binding["scope_id"] == "synthetic-project" and binding["user_id"] is None
    assert len(registered) == 1
    claim = next(row for row in added if isinstance(row, PromoClaim))
    claim.account_id = account.id + 1
    with pytest.raises(ValueError):
        provisioning._project_binding(tx, job, sub, job.payload)
    assert len(registered) == 1


@pytest.mark.parametrize("imported", [True, False])
def test_eligible_trial_is_real_claim_once_across_different_codes(gift_service, imported):
    service, account, promo, added, _ = gift_service
    promo["trial"] = True
    if imported:
        account.legacy_consent = {
            "legacy_free_trial_used": 0,
            "production_access": {
                "version": 1,
                "account_id": str(account.id),
                "state": "active",
                "source": "legacy_user_is_active",
                "source_account_id": str(account.id),
            },
        }
    Promos(service).claim(str(account.id), "GIFT")
    marker = next(row for row in added if isinstance(row, Setting))
    assert marker.value["claim_id"] == "genuine-claim"
    account.pending_promo = {"id": "different-claim", "code": "NEXT"}
    promo["code"] = "NEXT"
    with pytest.raises(DomainError):
        Promos(service).claim(str(account.id), "NEXT")
    assert len([row for row in added if isinstance(row, PromoClaim)]) == 1
