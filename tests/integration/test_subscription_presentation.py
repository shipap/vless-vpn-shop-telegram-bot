"""Subscription captions and display origins use owned facts without provider writes."""

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sunrise_infrastructure.db import Product, Setting, Subscription
from sunrise_infrastructure.instance_config import InstanceConfig
from sunrise_infrastructure.panel_v3 import production_binding_key

from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401
from tests.integration.test_panel_contact_sync import live_contacts  # noqa: F401
from tests.integration.test_scoped_panel_runtime_events import paid_panel, scoped_panel_database  # noqa: F401

PUBLIC = "https://connect.synthetic.invalid"


def configure_origin(config):
    config.subscription_public_origin = PUBLIC
    config.__dict__.pop("instance", None)


def test_dashboard_uses_current_catalog_title_and_display_origin_without_mutating_owned_facts(paid_panel):  # noqa: F811
    db, config, commerce, account, sid, _, _, calls = paid_panel
    configure_origin(config)
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        original = (sub.name, sub.connection_url, sub.short_uuid, sub.panel_user_id, sub.version)
        proof = deepcopy(session.get(Setting, "panel_test:" + sid).value)
        product = session.get(Product, sub.product_id)
        product.content = {"name": [{"type": "text", "text": "💎 Тариф из базы"}]}
    before = list(calls)
    card = commerce.dashboard(account)["subscriptions"][0]
    assert card["name"] == card["product_name"] == "💎 Тариф из базы"
    assert card["connection_url"] == PUBLIC + urlsplit(original[1]).path
    assert urlsplit(card["connection_url"]).path.rsplit("/", 1)[-1] == original[2]
    assert calls == before
    assert "connect.synthetic.invalid" not in commerce.panel_provider.subscription_hosts
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        assert (sub.name, sub.connection_url, sub.short_uuid, sub.panel_user_id, sub.version) == original
        assert session.get(Setting, "panel_test:" + sid).value == proof
        session.get(Product, sub.product_id).content = {"name": [{"type": "text", "text": "Новое имя"}]}
    assert commerce.dashboard(account)["subscriptions"][0]["name"] == "Новое имя"


def test_production_presentation_uses_exact_imported_binding_and_fresh_scope(live_contacts):  # noqa: F811
    db, commerce, _, sid, _, calls, _ = live_contacts
    commerce.public_settings = SimpleNamespace(
        instance=InstanceConfig(subscription_public_origin=PUBLIC),
        panel_subscription_allowed_hosts=["sub.synthetic.test"],
    )
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        original = sub.connection_url
        assert commerce._subscription_connection_url_in(session, sub) == PUBLIC + "/" + sub.short_uuid
        assert sub.connection_url == original
    with db.transaction() as session:
        proof = session.get(Setting, production_binding_key(sid))
        proof.value = proof.value | {"account_id": "99999999999999999"}
    with db.transaction() as session:
        assert commerce._subscription_connection_url_in(session, session.get(Subscription, sid)) is None
    assert calls == []


@pytest.mark.parametrize("invalid", ["short", "url", "admission"])
def test_public_projection_rechecks_owned_binding_and_current_admission(paid_panel, invalid):  # noqa: F811
    db, config, commerce, _, sid, _, _, calls = paid_panel
    configure_origin(config)
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        if invalid == "short":
            sub.short_uuid = "differentOwnedShort"
        elif invalid == "url":
            sub.connection_url = "https://foreign.synthetic.invalid/" + sub.short_uuid
        else:
            session.get(Setting, "scoped_test_admission").value = {}
    before = list(calls)
    with db.transaction() as session:
        assert commerce._subscription_connection_url_in(session, session.get(Subscription, sid)) is None
    assert calls == before


def test_connection_instructions_load_only_original_provider_origin(paid_panel):  # noqa: F811
    db, config, commerce, account, sid, _, _, calls = paid_panel
    configure_origin(config)
    fetched = []

    class Loader:
        def get(self, url):
            fetched.append(url)
            return []

    commerce.connection_provider = Loader()
    with db.transaction() as session:
        original = session.get(Subscription, sid).connection_url
    before = list(calls)
    assert commerce.connection_instructions(account, sid)["source"] == "fallback"
    assert fetched == [original]
    assert calls == before
