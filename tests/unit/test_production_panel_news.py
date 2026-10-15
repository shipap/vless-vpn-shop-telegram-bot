"""Production node/CRM paths; SQL cases use owned nonce schemas and fake HTTP."""

import hashlib
import hmac
import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.sql import operators
from sunrise_core.errors import DomainError
from sunrise_infrastructure import panel_news
from sunrise_infrastructure.db import (
    Account,
    Alias,
    Notification,
    NotificationDelivery,
    PanelEvent,
    Product,
    Setting,
    StaffRole,
    Subscription,
    now,
    uid,
)
from sunrise_infrastructure.notifications import NotificationService

NODE = "30000000-0000-4000-8000-000000000003"
STAMP = now().isoformat()


class RowSession:
    """Only the row repository boundary is replaced; route/owner/notification code is real."""

    def __init__(self):
        self.rows = {}

    def get(self, model, key, **kwargs):
        return self.rows.get((model, key))

    def add(self, row):
        key = (
            row.key
            if isinstance(row, Setting)
            else row.account_id
            if isinstance(row, StaffRole)
            else row.old_id
            if isinstance(row, Alias)
            else row.id
        )
        if key is None:
            row.id = key = uid()
        self.rows[type(row), key] = row

    def flush(self):
        pass

    def begin_nested(self):
        return nullcontext(self)

    def execute(self, statement, parameters):
        assert str(statement).startswith("SELECT pg_advisory_xact_lock(")

    def scalars(self, statement):
        description = statement.column_descriptions[0]
        model, projection = description["entity"], description["expr"]
        rows = [row for (kind, _), row in self.rows.items() if kind is model]
        for condition in statement._where_criteria:
            if not hasattr(condition, "left"):
                rows = [row for row in rows if getattr(row, condition.name)]
                continue
            value = getattr(condition.right, "value", None)
            name = condition.left.name

            def selected(row, condition=condition, name=name, value=value):
                current = self.get(StaffRole, row.id) if model is Account and name == "role" else row
                actual = getattr(current, name, None)
                if condition.operator is operators.eq:
                    return actual == value
                if condition.operator is operators.like_op:
                    return actual.startswith(value.rstrip("%"))
                raise AssertionError("unhandled synthetic repository predicate")

            rows = [row for row in rows if selected(row)]
        return rows if isinstance(projection, type) else [getattr(row, projection.key) for row in rows]

    def scalar(self, statement):
        return next(iter(self.scalars(statement)), None)


class AdmittedPolicy:
    production = True
    scoped_payments = False

    def __init__(self):
        self.allowed = True
        self.calls = []
        self.scope = SimpleNamespace(panel=SimpleNamespace(node_uuids=[NODE]))

    def require_current(self, session, capability=None):
        self.calls.append(capability)
        if not self.allowed:
            raise DomainError("production_stage_denied", "Этап не разрешён.", 409)


@pytest.fixture
def production(monkeypatch):
    session, runtime = RowSession(), AdmittedPolicy()
    settings = SimpleNamespace(
        runtime_mode="production",
        local_test=False,
        panel_test_node_uuids=[],
        owner_id="930000000001",
        admin_email_allowlist=["owner@example.test"],
        email_allowlist=[],
        test_telegram_recipients="",
        _runtime_policy=runtime,
    )
    panel = SimpleNamespace(
        contract="3.4_id",
        api_host="panel.example.test",
        nodes={NODE},
        client=SimpleNamespace(base_url="https://panel.example.test/api/"),
        runtime_policy=runtime,
    )
    routes = {
        "version": 1,
        "environment": "live",
        "panel_origin": "https://panel.example.test",
        "contract": "3.4_id",
        "nodes": [{"node_uuid": NODE, "product_ids": ["synthetic-plan"], "safe_label": "Сервер проекта"}],
        "infra": [
            {
                "provider_name": "synthetic-provider",
                "node_name": "synthetic-infra",
                "safe_label": "Сервер проекта",
            }
        ],
    }
    account = Account(
        id=301,
        telegram_id=930000000001,
        email="owner@example.test",
        email_verified=True,
        telegram_notifications_blocked=False,
    )
    session.add(account)
    session.add(StaffRole(account_id=account.id, role="owner"))
    session.add(Product(id="synthetic-plan", available=True))
    session.add(Setting(key="panel_event_routes", value=routes))
    session.add(Subscription(id="synthetic-owned-sub", account_id=account.id, product_id="synthetic-plan"))
    fenced = set()
    from sunrise_infrastructure import panel_events, retention

    # Provisioning ownership and archival fences have their own PG suites; no panel is contacted here.
    monkeypatch.setattr(panel_events.PanelEvents, "_owned", lambda self, txn, sub: None)
    monkeypatch.setattr(retention, "fenced_in", lambda txn, sub: sub.id in fenced)
    database = SimpleNamespace(transaction=lambda: nullcontext(session))
    commerce = SimpleNamespace(
        db=database,
        panel_provider=panel,
        public_settings=settings,
        _account=lambda txn, account_id: txn.get(Account, account_id),
    )
    sent = []
    notices = NotificationService(
        database,
        settings,
        telegram_transport=lambda recipient, payload, identifier: sent.append(
            (recipient, payload, identifier)
        ),
    )
    return SimpleNamespace(
        session=session,
        runtime=runtime,
        settings=settings,
        panel=panel,
        routes=routes,
        owner=account,
        commerce=commerce,
        notices=notices,
        fenced=fenced,
        sent=sent,
    )


def node_events():
    return [("event-outage", "synthetic-digest", "node.connection_lost", {"timestamp": STAMP})]


def crm_events():
    return [
        (
            "event-crm",
            "synthetic-digest",
            panel_news.CRM_EVENT,
            {"provider_name": "synthetic-provider", "node_name": "synthetic-infra", "next_billing_at": STAMP},
        )
    ]


def record_node(item):
    panel_news.record_node_news_in(
        item.session, item.panel, item.settings, NODE, {"status": "DISCONNECTED"}, node_events()
    )


def record_crm(item):
    return panel_news.crm_intent_in(item.session, item.panel, item.settings, crm_events(), item.notices)


def all_rows(item, kind):
    return [row for (model, _), row in item.session.rows.items() if model is kind]


def test_production_node_chronology_records_and_reads_owned_safe_news_without_alerts(production):
    item = production
    record_node(item)
    record_node(item)
    feed = panel_news.node_news(item.commerce, item.owner.id)
    assert len(feed["items"]) == 1
    assert feed["items"][0]["title"] == "Сервер проекта"
    assert feed["items"][0]["state"] == "outage"
    assert NODE not in json.dumps(feed) and "panel.example.test" not in json.dumps(feed)
    assert all_rows(item, Notification) == [] and all_rows(item, NotificationDelivery) == []
    assert item.runtime.calls == [None, None, None]


def test_production_crm_persists_one_owner_intent_and_delivers_through_current_gate(production):
    item = production
    assert record_crm(item) == {"status": "ok"}
    assert record_crm(item) == {"status": "ok"}
    intents = [row for row in all_rows(item, Setting) if row.key.startswith("infra_notice:")]
    notices, deliveries = all_rows(item, Notification), all_rows(item, NotificationDelivery)
    assert len(intents) == len(notices) == len(deliveries) == 1
    assert intents[0].value["environment"] == "live"
    notice, delivery = notices[0], deliveries[0]
    assert notice.account_id == item.owner.id and notice.audience == "owner"
    assert notice.kind == "infra_billing_due" and "неизвестна" in notice.body
    assert delivery.channel == "telegram" and delivery.recipient == str(item.owner.telegram_id)
    delivery.state = "in_flight"
    payload = {"notification_id": notice.id, "audience": "owner", "title": notice.title, "body": notice.body}
    item.notices._deliver("telegram", delivery.recipient, payload, delivery.id)
    assert item.sent == [(str(item.owner.telegram_id), payload, delivery.id)]
    assert item.runtime.calls[-1] == "notice_telegram"


def test_live_news_read_does_not_silently_drop_previously_recorded_live_event(production):
    item = production
    item.session.add(
        Setting(
            key="node_news:synthetic",
            value={
                "version": 1,
                "id": "synthetic-news",
                "node_uuid": NODE,
                "panel_origin": "https://panel.example.test",
                "environment": "live",
                "contract": "3.4_id",
                "state": "recovered",
                "occurred_at": STAMP,
            },
        )
    )
    assert [row["state"] for row in panel_news.node_news(item.commerce, item.owner.id)["items"]] == [
        "recovered"
    ]


@pytest.mark.parametrize("denial", ["gate", "origin", "node", "product"])
def test_production_route_denials_never_create_news_or_owner_intent(production, denial):
    item = production
    if denial == "gate":
        item.runtime.allowed = False
    elif denial == "origin":
        item.routes["panel_origin"] = "https://foreign.example.test"
    elif denial == "node":
        item.runtime.scope.panel.node_uuids = []
    else:
        item.session.get(Product, "synthetic-plan").available = False
    record_node(item)
    assert record_crm(item) == {"status": "denied"}
    assert not any(row.key.startswith(("node_news:", "infra_notice:")) for row in all_rows(item, Setting))
    assert all_rows(item, Notification) == []


@pytest.mark.parametrize("denial", ["unadmitted_telegram", "alias", "crm_pair"])
def test_owner_or_exact_crm_pair_denial_creates_no_notice(production, denial):
    item = production
    if denial == "unadmitted_telegram":
        item.owner.telegram_id = None
    elif denial == "alias":
        item.session.add(Alias(old_id=item.owner.id, canonical_id=999))
    else:
        item.routes["infra"][0]["node_name"] = "other-project-infra"
    assert record_crm(item) == (
        {"status": "denied"}
        if denial == "crm_pair"
        else {"status": "error", "error": "owner_notice_unavailable"}
    )
    assert all_rows(item, Notification) == [] and all_rows(item, NotificationDelivery) == []


@pytest.mark.parametrize("denial", ["fenced", "different_product", "foreign_origin", "foreign_environment"])
def test_news_feed_excludes_fenced_or_unrelated_project_facts(production, denial):
    item = production
    item.session.add(
        Setting(
            key="node_news:synthetic",
            value={
                "version": 1,
                "id": "synthetic-news",
                "node_uuid": NODE,
                "panel_origin": "https://panel.example.test",
                "environment": "live",
                "contract": "3.4_id",
                "state": "outage",
                "occurred_at": STAMP,
            },
        )
    )
    if denial == "fenced":
        item.fenced.add("synthetic-owned-sub")
    elif denial == "different_product":
        item.session.get(Subscription, "synthetic-owned-sub").product_id = "other-plan"
    elif denial == "foreign_origin":
        item.session.get(Setting, "node_news:synthetic").value["panel_origin"] = (
            "https://foreign.example.test"
        )
    else:
        item.session.get(Setting, "node_news:synthetic").value["environment"] = "sandbox"
    assert panel_news.node_news(item.commerce, item.owner.id)["items"] == []


def test_current_owner_permission_revocation_blocks_production_crm_delivery(production):
    item = production
    assert record_crm(item) == {"status": "ok"}
    notice, delivery = all_rows(item, Notification)[0], all_rows(item, NotificationDelivery)[0]
    delivery.state = "in_flight"
    item.owner.telegram_id = None
    with pytest.raises(DomainError):
        item.notices._deliver(
            "telegram", delivery.recipient, {"notification_id": notice.id, "audience": "owner"}, delivery.id
        )
    assert item.sent == []


def test_current_telegram_owner_does_not_need_a_staff_email(production):
    item = production
    item.owner.email, item.owner.email_verified = None, False
    item.settings.admin_email_allowlist = []
    assert record_crm(item) == {"status": "ok"}


def production_signed(kind, data, settings):
    stamp = now().isoformat()
    raw = json.dumps({"scope": kind.split(".")[0], "event": kind, "timestamp": stamp, "data": data}).encode()
    return raw, {
        "x-remnawave-signature": hmac.new(
            settings.panel_webhook_secret.encode(), raw, hashlib.sha256
        ).hexdigest(),
        "x-remnawave-timestamp": stamp,
    }


@pytest.fixture
def pg_production():
    from sunrise_infrastructure.panel_events import PanelEvents
    from sunrise_infrastructure.panel_v3 import RemnawaveProduction, production_binding_key

    from tests.isolated_database import isolated_database
    from tests.unit.test_deferred_archive_owner_notice import CurrentPolicy, seed_archive_inputs

    with isolated_database("production_node_crm") as db:
        runtime = CurrentPolicy()
        runtime.scoped_payments = False
        squad = "40000000-0000-4000-8000-000000000004"
        runtime.scope.panel = SimpleNamespace(
            origin="https://panel.example.test",
            contract="3.4_id",
            node_uuids=[NODE],
            internal_squads=[squad],
            external_squads=[],
            subscription_hosts=["sub.example.test"],
        )
        settings = SimpleNamespace(
            runtime_mode="production",
            local_test=False,
            owner_id=None,
            admin_telegram_allowlist=["930000000001"],
            admin_email_allowlist=["owner@example.test"],
            email_allowlist=[],
            test_telegram_recipients="",
            _runtime_policy=runtime,
            panel_test_node_uuids=[],
            panel_webhook_secret=uid(),
            panel_webhook_max_bytes=10000,
            panel_webhook_max_age_seconds=300,
        )
        routes = {
            "version": 1,
            "environment": "live",
            "panel_origin": runtime.scope.panel.origin,
            "contract": "3.4_id",
            "nodes": [{"node_uuid": NODE, "product_ids": ["synthetic-plan"], "safe_label": "Сервер проекта"}],
            "infra": [
                {
                    "provider_name": "synthetic-provider",
                    "node_name": "synthetic-infra",
                    "safe_label": "Сервер проекта",
                }
            ],
        }
        node = {
            "uuid": NODE,
            "name": "private-infrastructure-name",
            "isConnected": False,
            "isDisabled": False,
            "countryCode": "ZZ",
        }
        requests, sent = [], []

        def http(request):
            requests.append((request.method, request.url.path))
            if request.method == "GET" and request.url.path == "/api/internal-squads/" + squad + "/accessible-nodes":
                return httpx.Response(200, json={"response": {
                    "squadUuid": squad, "accessibleNodes": [{"uuid": NODE}],
                }})
            assert (request.method, request.url.path) == ("GET", "/api/nodes/" + NODE)
            return httpx.Response(200, json={"response": node})

        panel = RemnawaveProduction(runtime_policy=runtime, token=uid(), transport=httpx.MockTransport(http))
        try:
            with db.transaction() as session:
                sub, binding, proof = seed_archive_inputs(session, owner_verified=True)
                for product in session.scalars(select(Product)):
                    product.available = product.id == "synthetic-plan"
                    if product.available:
                        product.internal_squad_uuid = squad
                sub.short_uuid = "syntheticshortuuid123"
                record = session.get(Setting, production_binding_key(sub.id))
                record.value = proof | {"binding": binding | {"short_uuid": sub.short_uuid}}
                session.get(Account, 901).telegram_id = 930000000001
                session.add(Setting(key="panel_event_routes", value=routes))
            notices = NotificationService(db, settings, telegram_transport=lambda *args: sent.append(args))
            commerce = SimpleNamespace(
                db=db,
                panel_provider=panel,
                public_settings=settings,
                _account=lambda txn, aid: txn.get(Account, aid),
            )
            yield SimpleNamespace(
                db=db,
                runtime=runtime,
                settings=settings,
                panel=panel,
                events=PanelEvents(db, panel, settings, notices),
                commerce=commerce,
                notices=notices,
                routes=routes,
                node=node,
                requests=requests,
                sent=sent,
                squad=squad,
            )
        finally:
            panel.client.close()


@pytest.mark.postgres
def test_pg_live_node_signed_ingest_worker_and_actual_owned_news(pg_production):
    item = pg_production
    raw, headers = production_signed("node.connection_lost", item.node, item.settings)
    assert item.events.accept(raw, headers)["status"] == "accepted"
    assert item.events.reconcile_pending() == 1
    assert item.events.accept(raw, headers)["status"] == "duplicate"
    assert [row["state"] for row in panel_news.node_news(item.commerce, 301)["items"]] == ["outage"]
    assert item.panel.registered  # actual _owned validated and registered the DB binding
    assert item.requests == [("GET", "/api/nodes/" + NODE)]
    assert panel_news.node_news(item.commerce, 901)["items"] == []
    with item.db.transaction() as session:
        assert list(session.scalars(select(Notification))) == []
        assert list(session.scalars(select(NotificationDelivery))) == []
        session.get(Setting, "panel_event_routes").value = item.routes | {"nodes": []}
    assert (
        item.events.accept(*production_signed("node.connection_restored", item.node, item.settings))["status"]
        == "accepted"
    )
    assert len(item.requests) == 1  # ingest never refreshes the daily group cache.
    assert item.events.reconcile_pending() == 0
    assert len(item.requests) == 1


@pytest.mark.postgres
def test_pg_live_crm_keeps_admitted_event_until_verified_owner_and_rechecks_delivery(pg_production):
    item = pg_production
    with item.db.transaction() as session:
        session.get(Account, 901).telegram_id = None
    data = {
        "providerName": "synthetic-provider",
        "nodeName": "synthetic-infra",
        "nextBillingAt": now().isoformat(),
        "loginUrl": "https://private.example.test/never-store",
    }
    raw, headers = production_signed(panel_news.CRM_EVENT, data, item.settings)
    assert item.events.accept(raw, headers)["status"] == "accepted"
    assert item.events.reconcile_pending() == 0
    with item.db.transaction() as session:
        event = session.scalar(select(PanelEvent))
        assert event.state == "pending" and event.last_error == "owner_notice_unavailable"
        assert "loginUrl" not in json.dumps(event.payload)
        assert list(session.scalars(select(Notification))) == []
        session.get(Account, 901).telegram_id = 930000000001  # synthetic admitted identity restored
        event.available_at = now()
    assert item.events.reconcile_pending() == 1
    assert item.events.accept(raw, headers)["status"] == "duplicate"
    with item.db.transaction() as session:
        notice = session.scalar(select(Notification))
        delivery = session.scalar(select(NotificationDelivery))
        assert notice.account_id == 901 and notice.audience == "owner" and notice.kind == "infra_billing_due"
        assert "https://" not in notice.body
        assert delivery.channel == "telegram"
        delivery.state = "in_flight"
        payload = {
            "notification_id": notice.id,
            "audience": "owner",
            "title": notice.title,
            "body": notice.body,
        }
        delivery_id, recipient = delivery.id, delivery.recipient
        assert session.scalar(select(PanelEvent)).state == "done"
    item.notices._deliver("telegram", recipient, payload, delivery_id)
    assert item.sent == [(recipient, payload, delivery_id)]
    with item.db.transaction() as session:
        session.get(Account, 901).telegram_id = None
    with pytest.raises(DomainError):
        item.notices._deliver("telegram", recipient, payload, delivery_id)
    assert len(item.sent) == 1 and item.requests == []


@pytest.mark.postgres
@pytest.mark.parametrize("revoked", ["gate", "scope_node", "panel_node"])
def test_pg_live_node_rechecks_scope_and_routes_after_signed_ingest(pg_production, revoked):
    item = pg_production
    assert (
        item.events.accept(*production_signed("node.connection_lost", item.node, item.settings))["status"]
        == "accepted"
    )
    if revoked == "gate":
        item.runtime.allowed = False
    elif revoked == "scope_node":
        item.runtime.scope.panel.node_uuids = []
    elif revoked == "panel_node":
        item.panel.nodes.clear()
    assert item.events.reconcile_pending() == 0
    assert item.requests == [] and item.sent == []
    with item.db.transaction() as session:
        assert session.scalar(select(PanelEvent)).state == "ignored"
        assert list(session.scalars(select(Notification))) == []


@pytest.mark.postgres
@pytest.mark.parametrize("changed", ["route", "product"])
def test_pg_live_node_rechecks_cached_routes_and_catalog_without_group_refresh(pg_production, changed):
    item = pg_production
    assert item.events.accept(*production_signed("node.connection_lost", item.node, item.settings))["status"] == "accepted"
    with item.db.transaction() as session:
        if changed == "route":
            session.get(Setting, "panel_event_routes").value = item.routes | {"nodes": []}
        else:
            session.get(Product, "synthetic-plan").available = False
    assert item.events.reconcile_pending() == 0
    assert item.sent == [] and item.requests == []
    with item.db.transaction() as session:
        assert session.scalar(select(PanelEvent)).state == "ignored"
        assert list(session.scalars(select(Notification))) == []
        routes = session.get(Setting, "panel_event_routes").value
        assert bool(routes["nodes"]) is (changed == "product")
    assert panel_news.node_news(item.commerce, 301)["items"] == []


@pytest.mark.postgres
def test_pg_live_all_node_events_use_cached_routes_without_group_reads(pg_production):
    item = pg_production
    for kind in ("created", "modified", "disabled", "enabled", "deleted", "connection_lost", "connection_restored", "traffic_notify"):
        before = len(item.requests)
        assert item.events.accept(*production_signed("node." + kind, item.node, item.settings))["status"] == "accepted"
        assert item.events.reconcile_pending() == 1
        assert item.requests[before:] == ([] if kind == "deleted" else [("GET", "/api/nodes/" + NODE)])
        with item.db.transaction() as session:
            assert session.get(Setting, "panel_event_routes").value == item.routes
    assert item.sent == []


def test_no_owner_does_not_retain_foreign_crm_pair(production):
    item = production
    item.owner.email_verified = False
    item.routes["infra"][0]["node_name"] = "foreign-project-infra"
    assert record_crm(item) == {"status": "denied"}
    assert all_rows(item, Notification) == []
