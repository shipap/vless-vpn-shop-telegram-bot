"""Неподтверждённые согласия не занимают окно обработки текущей кассы."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Agreement, Setting, Subscription, now
from sunrise_infrastructure.recurring import Recurring

from tests.isolated_database import isolated_database


class SyntheticExistingProvider:
    environment = "live"
    terminal = "123"

    def __init__(self):
        self.calls = []

    def agreement(self, identifier):
        self.calls.append(("status", identifier))
        return {"billing_status": "enabled"}

    def stop_agreement(self, identifier):
        self.calls.append(("cancel", identifier))
        return {"state": "stop", "billing_status": "stopped"}


class SyntheticPolicy:
    scope = SimpleNamespace(scope_id="123")

    def __init__(self, revoked=False):
        self.revoked = revoked
        self.calls = []

    def require(self, capability=None):
        self.calls.append(capability)
        if self.revoked:
            raise DomainError("production_stage_denied", "Операция недоступна.", 503)

    def require_current(self, session, capability=None):
        self.require(capability)


def add_agreement(session, identifier, consent, *, state="enabled", minutes_old=1):
    session.add(Subscription(id="sub:" + identifier, account_id=1, devices_limit=4))
    session.flush()
    session.add(
        Agreement(
            id=identifier,
            subscription_id="sub:" + identifier,
            account_id=1,
            provider_id="provider:" + identifier,
            state=state,
            consent_amount="99.00",
            currency="RUB",
            consent=consent,
            created_at=now() - timedelta(minutes=minutes_old),
        )
    )


@pytest.mark.parametrize("state", ["enabled", "stop_pending"])
@pytest.mark.parametrize("revoked", [False, True])
def test_live_scope_filters_unproven_agreements_before_limit_and_rechecks_capabilities(state, revoked):
    with isolated_database("recurring_scope") as db:
        commerce = Commerce(db, True)
        commerce.runtime_policy = SyntheticPolicy(revoked)
        provider = SyntheticExistingProvider()
        service = Recurring(commerce, provider)
        valid = {"environment": "live", "provider": "rollypay", "terminal": "123", "scope_id": "123"}
        unproven = [
            {},
            valid | {"environment": "sandbox"},
            valid | {"provider": "other"},
            valid | {"terminal": "other"},
            valid | {"scope_id": "other"},
            valid | {"terminal": 123},
            valid | {"scope_id": 123},
            [],
        ]
        with db.transaction() as session:
            session.add(Account(id=1))
            session.flush()
            session.get(Setting, "recurring").value = {"enabled": False}
            for index, terms in enumerate(unproven):
                add_agreement(session, "foreign_" + str(index), deepcopy(terms), state=state, minutes_old=20)
            add_agreement(session, "own_ready", valid, state=state)
        service.reconcile(limit=1)
        capability = "existing_recurring_cancel" if state == "stop_pending" else "existing_recurring_status"
        assert commerce.runtime_policy.calls and set(commerce.runtime_policy.calls) == {capability}
        assert provider.calls == (
            [] if revoked else [("cancel" if state == "stop_pending" else "status", "provider:own_ready")]
        )
        with db.transaction() as session:
            for index, terms in enumerate(unproven):
                row = session.get(Agreement, "foreign_" + str(index))
                assert row.consent == terms and row.state == state
            own = session.get(Agreement, "own_ready")
            assert own.state == ("stopped" if not revoked and state == "stop_pending" else state)
            assert ("last_checked_at" in own.consent) is not revoked
            assert session.get(Setting, "recurring").value == {"enabled": False}


def test_sandbox_reconciliation_keeps_existing_local_agreement_behavior():
    with isolated_database("recurring_scope") as db:
        commerce = Commerce(db, True)
        provider = SyntheticExistingProvider()
        provider.environment = "sandbox"
        with db.transaction() as session:
            session.add(Account(id=1))
            session.flush()
            add_agreement(session, "sandbox_existing", {})
        Recurring(commerce, provider).reconcile(limit=1)
        assert provider.calls == [("status", "provider:sandbox_existing")]
