"""A scoped panel refresh must ignore bound cards from the old fake test panel."""

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

from sunrise_core.errors import DomainError
from sunrise_infrastructure import retention
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Base, Database, Product, Subscription


def test_scoped_dashboard_refresh_selects_only_scoped_live_panel_identities(monkeypatch):
    old_card = SimpleNamespace(
        id="legacy-card", account_id=1, environment="local_test", panel_uuid=None, panel_user_id=123
    )
    current_card = SimpleNamespace(
        id="new-card", account_id=1, environment="scoped_live", panel_uuid=None, panel_user_id=456
    )

    class Session:
        def scalars(self, query):
            return [old_card, current_card]

    class Database:
        @contextmanager
        def transaction(self):
            yield Session()

    commerce = Commerce(
        Database(),
        local_test=True,
        runtime_policy=SimpleNamespace(production=False, scoped_payments=True),
    )
    monkeypatch.setattr(commerce, "_account", lambda session, account_id: SimpleNamespace(id=1))
    monkeypatch.setattr(retention, "fenced_in", lambda session, sub: False)

    # With no panel configured, each selected ID is exposed as an unavailable refresh.
    # The legacy card must remain outside that real-panel candidate set.
    assert commerce._refresh_panel(1) == {
        "new-card": {"status": "error", "error": "panel_unavailable"}
    }


def test_scoped_dashboard_can_open_own_legacy_card_without_registering_it_in_real_panel(monkeypatch):
    old_card = SimpleNamespace(
        id="legacy-card", account_id=1, environment="local_test", panel_uuid=None, panel_user_id=123
    )

    class Session:
        def scalar(self, query):
            return old_card

    class Database:
        @contextmanager
        def transaction(self):
            yield Session()

    def deny_real_panel_subscription(session, sub):
        raise DomainError("scoped_test_subscription_denied", "Тестовая подписка не подтверждена.", 403)

    policy = SimpleNamespace(
        production=False,
        scoped_payments=True,
        require_current=lambda session: None,
        require_subscription_in=deny_real_panel_subscription,
    )
    commerce = Commerce(Database(), local_test=True, runtime_policy=policy)
    monkeypatch.setattr(commerce, "_account", lambda session, account_id: SimpleNamespace(id=1))

    assert commerce._refresh_panel(1, "legacy-card") == {}


def test_scoped_dashboard_keeps_legacy_card_readable_without_real_panel_authority(monkeypatch):
    database = Database("sqlite://")
    Base.metadata.create_all(database.engine)
    with database.transaction() as session:
        session.add(Account(id=1, telegram_id=23456))
        session.add(
            Product(
                id="standard", code="standard", name="Стандарт", tier_level=1,
                price=Decimal("99.00"), currency="RUB", included_devices=1,
            )
        )
        session.add(
            Subscription(
                id="legacy-card", account_id=1, product_id="standard", name="Старая тестовая",
                environment="local_test", status="active", panel_user_id=123, devices_limit=1,
            )
        )

    def deny_real_panel_subscription(session, sub):
        raise DomainError("scoped_test_subscription_denied", "Тестовая подписка не подтверждена.", 403)

    policy = SimpleNamespace(
        production=False, scoped_payments=True, require_account_in=lambda session, account: None,
        require_current=lambda session: None, require_subscription_in=deny_real_panel_subscription,
    )
    panel = SimpleNamespace(contract="3.4_id", scoped_runtime_policy=policy)
    commerce = Commerce(
        database, local_test=True, runtime_policy=policy, panel_provider=panel,
        public_settings=SimpleNamespace(panel_subscription_allowed_hosts=[]),
    )
    # Referral totals use PostgreSQL split_part; this test isolates subscription projection.
    monkeypatch.setattr(commerce, "_referral_info", lambda session, account: {})

    dashboard = commerce.dashboard(1, refresh_panel=True)
    assert [sub["id"] for sub in dashboard["subscriptions"]] == ["legacy-card"]
