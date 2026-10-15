"""Staff notices identify email-only buyers without trusting stale Telegram names."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sunrise_infrastructure.notifications import validate_telegram_content
from sunrise_infrastructure.sale_notifications import paid_staff_sale_content


@pytest.mark.parametrize(
    ("telegram_id", "username", "email", "verified", "expected"),
    [
        (None, "stale_user", "buyer@example.invalid", True, "buyer@example.invalid"),
        (None, "stale_user", "buyer@example.invalid", False, "Telegram не привязан"),
        (None, None, None, True, "Telegram не привязан"),
        (100001, "bound_user", "buyer@example.invalid", True, "@bound_user (ID: 100001)"),
        (100001, None, "buyer@example.invalid", True, "ID: 100001"),
    ],
)
def test_paid_notice_identity_prioritizes_bound_telegram_then_verified_email(
    telegram_id, username, email, verified, expected
):
    account = SimpleNamespace(
        telegram_id=telegram_id, telegram_username=username, email=email, email_verified=verified
    )
    title, body = paid_staff_sale_content(account, SimpleNamespace(amount=Decimal("1"), currency="RUB"), {})
    assert body.splitlines()[0] == "Покупатель: " + expected
    assert "stale_user" not in body
    if not verified or telegram_id is not None:
        assert "buyer@example.invalid" not in body
    assert "ID: None" not in body
    assert "Покупатель:" in validate_telegram_content(title, body)


def test_email_identity_is_escaped_by_the_real_telegram_renderer():
    account = SimpleNamespace(
        telegram_id=None, telegram_username=None, email="<buyer>@example.invalid", email_verified=True
    )
    title, body = paid_staff_sale_content(account, SimpleNamespace(amount=Decimal("1"), currency="RUB"), {})
    rendered = validate_telegram_content(title, body)
    assert "&lt;buyer&gt;@example.invalid" in rendered
    assert "<buyer>" not in rendered


@pytest.mark.parametrize("verified", [True, False])
def test_owner_deletion_notice_resolves_archived_account_for_email_identity(verified):
    from sunrise_infrastructure.db import Account
    from sunrise_infrastructure.subscription_archive import ArchiveState, owner_deletion_intent_in

    account = SimpleNamespace(
        telegram_id=None,
        telegram_username="stale_user",
        email="buyer@example.invalid",
        email_verified=verified,
    )
    state = ArchiveState.model_construct(
        owner_id="100001",
        subscription_id="synthetic-subscription",
        reason="expiry_cleanup",
        subscription_name="Подписка",
        tariff_name="Тариф",
        device_limit=1,
    )
    emitted = []

    def get(model, identifier):
        assert model is Account and str(identifier) == "100001"
        return account

    def emit_in(session, owner_id, event_key, kind, title, body, **kwargs):
        emitted.append(body)
        return SimpleNamespace(id="synthetic-notice")

    result = owner_deletion_intent_in(
        SimpleNamespace(get=get), SimpleNamespace(emit_in=emit_in), SimpleNamespace(id=100002), state
    )
    assert result.owner_intent_id == "synthetic-notice"
    expected = "buyer@example.invalid" if verified else "Telegram не привязан"
    assert emitted[0].splitlines()[0] == "Пользователь: " + expected
    assert "stale_user" not in emitted[0]


def test_existing_owner_deletion_intent_does_not_reread_account_or_emit_again():
    from sunrise_infrastructure.subscription_archive import ArchiveState, owner_deletion_intent_in

    state = ArchiveState.model_construct(owner_intent_id="synthetic-existing-notice")
    assert owner_deletion_intent_in(None, None, None, state) is state
