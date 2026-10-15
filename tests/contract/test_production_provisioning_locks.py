from types import SimpleNamespace

import pytest
from sunrise_infrastructure.db import Account, Job, Subscription
from sunrise_infrastructure.panel import PanelReview
from sunrise_infrastructure.provisioning import Provisioning


@pytest.mark.parametrize("original", [10, 9007199254740993])
def test_production_mutation_lock_domain_resolves_alias_before_account_subscription_job(original):
    assert hasattr(Provisioning, "_production_rows"), "Production lock order helper is missing"
    canonical = 9007199254740993
    order = []
    account = SimpleNamespace(id=canonical)
    sub = SimpleNamespace(id="own", account_id=canonical)
    job = SimpleNamespace(id="job", account_id=canonical)
    rows = {Account: account, Subscription: sub, Job: job}

    def scalar(query):
        entity = query.column_descriptions[0]["entity"]
        order.append(entity.__name__)
        return rows[entity]

    session = SimpleNamespace(get=lambda model, key, **_: rows[model], scalar=scalar)

    def lock_accounts(session, ids):
        assert set(ids) == {original, canonical}
        order.append("Account")
        return {key: canonical for key in ids}

    service = Provisioning(
        SimpleNamespace(),
        SimpleNamespace(runtime_policy=SimpleNamespace(production=True, require_current=lambda *args: None)),
        commerce=SimpleNamespace(_lock_accounts_in=lock_accounts),
    )
    assert service._production_rows(session, "job", "own", original) == (account, sub, job)
    assert order == ["Account", "Subscription", "Job"]


def test_production_mutation_different_canonical_owner_denied_after_sorted_locks():
    assert hasattr(Provisioning, "_production_rows"), "Production lock order helper is missing"
    account = SimpleNamespace(id=10)
    sub = SimpleNamespace(id="own", account_id=20)
    job = SimpleNamespace(id="job", account_id=20)
    rows = {Account: account, Subscription: sub, Job: job}
    session = SimpleNamespace(
        get=lambda model, key, **_: rows[model],
        scalar=lambda query: rows[query.column_descriptions[0]["entity"]],
    )
    service = Provisioning(
        None,
        SimpleNamespace(runtime_policy=SimpleNamespace(production=True, require_current=lambda *args: None)),
        commerce=SimpleNamespace(_lock_accounts_in=lambda s, ids: {value: value for value in ids}),
    )
    with pytest.raises(PanelReview):
        service._production_rows(session, "job", "own", 10)


def test_disabled_customer_cannot_cross_fresh_mutation_preflight_but_known_effect_recovery_keeps_identity():
    from sunrise_core.errors import DomainError

    account = Account(
        id=9007199254740993,
        legacy_consent={
            "production_access": {
                "version": 1,
                "account_id": "9007199254740993",
                "state": "disabled",
                "source": "legacy_user_is_active",
                "source_account_id": "9007199254740993",
            }
        },
    )
    sub = SimpleNamespace(id="own", account_id=account.id)
    job = SimpleNamespace(id="job", account_id=account.id)
    rows = {Account: account, Subscription: sub, Job: job}
    session = SimpleNamespace(
        get=lambda model, key, **_: rows[model],
        scalar=lambda query: rows[query.column_descriptions[0]["entity"]],
    )
    policy = SimpleNamespace(production=True, require_current=lambda *args: None)
    service = Provisioning(
        None,
        SimpleNamespace(runtime_policy=policy),
        commerce=SimpleNamespace(_lock_accounts_in=lambda s, ids: {value: account.id for value in ids}),
    )
    with pytest.raises(DomainError) as failure:
        service._production_rows(session, "job", "own", account.id, customer=True)
    assert failure.value.status == 404
    assert service._production_rows(session, "job", "own", account.id) == (account, sub, job)
