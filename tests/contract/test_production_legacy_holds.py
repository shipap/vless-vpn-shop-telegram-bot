from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure import retention
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Setting, Subscription


@pytest.mark.parametrize("key", ["legacy_operation:synthetic", "legacy_operation_account:9007199254740993"])
@pytest.mark.parametrize("value", [{"state": "review"}, None, {"state": "resolved"}])
def test_any_root_legacy_operation_marker_fences_new_mutation_and_retention(key, value):
    sub = Subscription(id="synthetic", account_id=9007199254740993, environment="live")
    session = SimpleNamespace(
        get=lambda model, identifier, **_: (
            Setting(key=key, value=value) if model is Setting and identifier == key else None
        )
    )
    session.scalar = lambda query: sub

    @contextmanager
    def transaction():
        yield session

    commerce = Commerce(SimpleNamespace(transaction=transaction), local_test=True)
    with pytest.raises(DomainError) as failure:
        commerce._assert_subscription_operations(session, sub)
    assert failure.value.code == "legacy_operation_review"
    assert "поддерж" in failure.value.message.casefold()
    assert retention.fenced_in(session, sub)
    assert commerce.connection_instructions(str(sub.account_id), sub.id)["source"] == "fallback"
