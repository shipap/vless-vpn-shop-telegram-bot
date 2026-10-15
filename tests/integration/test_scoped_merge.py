"""A verified scoped merge preserves original bank and panel facts."""

import hashlib
import json
import re
from datetime import timedelta
from uuid import uuid4

import fakeredis
import httpx
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Alias,
    Order,
    Payment,
    ProviderOperation,
    Setting,
    Subscription,
    now,
)
from sunrise_infrastructure.mail import configured_mail
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.panel import configured_panel
from sunrise_infrastructure.security import RateGuard
from sunrise_infrastructure.settings import RuntimePolicy, Settings

from tests.contract.test_production_transports import SyntheticSMTP
from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401 -- pytest fixture
from tests.integration.test_scoped_real_invoices import (  # noqa: F401 -- pytest fixture
    prepared_invoice,
    scoped_database,
)
from tests.legal_fixture import current_test_legal


@pytest.fixture
def merge_context(scoped_database):  # noqa: F811 -- fixture dependency
    db, initial, old_policy, _, target_id = scoped_database
    with db.transaction() as session:
        session.get(Account, target_id).email = None
    data = json.loads(old_policy.path.read_text(encoding="utf-8"))
    smtp_scope = {
        "host": "smtp.synthetic.invalid",
        "port": 587,
        "mode": "starttls",
        "sender": "auth@synthetic.invalid",
    }
    data.update(allow_email_signup=True, smtp=smtp_scope)
    old_policy.path.write_text(json.dumps(data), encoding="utf-8")
    config = Settings(
        _env_file=None,
        **(
            initial.model_dump()
            | {
                "smtp_test_authorized": True,
                "smtp_host": smtp_scope["host"],
                "smtp_port": 587,
                "smtp_mode": "starttls",
                "smtp_sender": smtp_scope["sender"],
                "smtp_allowed_hosts": [smtp_scope["host"]],
                "scoped_test_admission_sha256": hashlib.sha256(old_policy.path.read_bytes()).hexdigest(),
            }
        ),
    )
    policy = RuntimePolicy.for_settings(config)
    with db.transaction() as session:
        session.get(Setting, "scoped_test_admission").value = policy._marker()
    policy.admit(db)
    object.__setattr__(config, "_runtime_policy", policy)
    smtp = SyntheticSMTP()
    accounts = Accounts(
        db,
        RateGuard(fakeredis.FakeRedis()),
        config,
        mail_transport=configured_mail(config, factory=lambda **_: smtp),
    )
    challenge = accounts.request_code("merge-customer@external.invalid", "127.0.0.1")
    code = re.search(
        r"\b[0-9]{6}\b", smtp.messages[-1].get_body(preferencelist=("plain",)).get_content()
    ).group()
    token, auth = accounts.verify_code(
        challenge["challenge_id"],
        code,
        "127.0.0.1",
        legal_accepted=True,
        documents=current_test_legal(Commerce(db)),
    )
    yield db, config, policy, accounts, token, auth, target_id


@pytest.mark.parametrize("buyer,revoke_contacts", [("telegram", False), ("email", False), ("telegram", True)])
def test_scoped_merge_keeps_session_and_immutable_paid_panel_facts(merge_context, buyer, revoke_contacts):
    db, config, policy, accounts, token, auth, target_id = merge_context
    source_id = int(auth["account"]["id"])
    original_payer = target_id if buyer == "telegram" else source_id
    users, calls, revoke_on_get = {}, [], [False]

    def panel_request(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and revoke_on_get[0]:
            with db.transaction() as session:
                marker = session.get(Setting, "scoped_test_admission")
                marker.value = marker.value | {"scope_id": "revoked"}
            revoke_on_get[0] = False
        path = request.url.path.removeprefix("/api/")
        if request.method == "POST":
            body = json.loads(request.content)
            user = body | {
                "id": 901,
                "activeInternalSquads": [{"uuid": body["activeInternalSquads"][0]}],
                "subscriptionUrl": "https://vpn.synthetic.invalid/sub/" + body["shortUuid"],
                "userTraffic": {"usedTrafficBytes": 0},
                "createdAt": now().isoformat(),
            }
            users[901] = user
        if request.method == "PATCH":
            body = json.loads(request.content)
            users[body["id"]].update(body)
            if "activeInternalSquads" in body:
                users[body["id"]]["activeInternalSquads"] = [
                    {"uuid": x} for x in body["activeInternalSquads"]
                ]
        if path == "hwid/devices/901":
            return httpx.Response(200, json={"response": {"devices": [], "total": 0}})
        if path == "users/901" or request.method in {"POST", "PATCH"}:
            return httpx.Response(200, json={"response": users[901]})
        return httpx.Response(404)

    config.panel_token = "synthetic-panel-key"  # noqa: S105 -- in-memory provider only
    panel = configured_panel(config, transport=httpx.MockTransport(panel_request))
    commerce = Commerce(
        db, local_test=True, public_settings=config, runtime_policy=policy, panel_provider=panel
    )
    provisioning = commerce.provisioning
    order, billing, provider, bank_calls, bank_response = prepared_invoice(
        (db, config, policy, commerce, original_payer)
    )
    billing.poll_known_payments()
    assert billing.reconcile_inbox(provider) == 1
    assert provisioning.run_jobs() == 1
    with db.transaction() as session:
        sub = session.scalar(select(Subscription))
        sid = sub.id
        original = dict(session.get(Setting, "panel_test:" + sid).value)
        original_request = dict(session.scalar(select(ProviderOperation)).request)
        original_expiry, original_version = sub.expires_at, sub.version
    _, initiating_session = accounts.session(token)
    proof = accounts.request_link(
        str(source_id),
        policy.scope.telegram_id,
        initiating_session.email_confirmed_at,
        session_id=initiating_session.id,
        ip="127.0.0.1",
    )["proof"]
    result = accounts.confirm_telegram_link(str(target_id), proof)
    assert result["id"] == str(source_id)
    actor, kept_session = accounts.session(token)
    assert actor.id == source_id and kept_session.id == initiating_session.id
    with db.transaction() as session:
        assert session.get(Account, target_id) is None
        assert session.get(Alias, target_id).canonical_id == source_id
        sub = session.get(Subscription, sid)
        assert sub.account_id == source_id
        assert (sub.expires_at, sub.version) == (original_expiry, original_version)
        assert session.get(Setting, "panel_test:" + sid).value == original
        assert session.scalar(select(ProviderOperation)).request == original_request
        payment = session.scalar(select(Payment))
        assert payment.account_id == source_id and payment.original_account_id == original_payer
        policy.require_subscription_in(session, sub)
        policy.require_order_in(session, session.get(Order, order["id"]))
        paid_notice = NotificationService(db, config).emit_in(
            session,
            source_id,
            "payment:" + order["id"],
            "payment_received",
            "Заказ оплачен",
            "Доступ подтверждён.",
            channel="inbox",
        )
        assert NotificationService(db, config)._scoped_paid_order_in(
            session, session.get(Account, source_id), paid_notice
        )
    # An original payer remains the provider customer after canonical ownership transfer.
    provider.payment(bank_response["payment_id"])
    quote = commerce.quote(source_id, "standard", 30, "renew", subscription_id=sid, payment_method="sbp")
    assert quote["id"]
    from sunrise_infrastructure.panel_contacts import PanelContactSync

    if revoke_contacts:
        before = list(calls)
        revoke_on_get[0] = True
        assert PanelContactSync(commerce).run_pending() == 0
        assert calls == before + [("GET", "/api/users/901")]
        assert "email" not in users[901]
        panel.client.close()
        provider.client.close()
        return
    assert PanelContactSync(commerce).run_pending() == 1
    assert users[901]["telegramId"] == int(policy.scope.telegram_id)
    assert users[901]["email"] == "merge-customer@external.invalid"
    assert users[901]["username"] == original["binding"]["username"]
    renewal = commerce.create_order(source_id, quote["id"], uuid4().hex)
    bank_response.update(
        order_id=renewal["id"],
        amount=renewal["total"],
        payment_id="merge-renew-" + uuid4().hex,
        customer_id=str(source_id),
        created_at=now().isoformat(),
        expires_at=(now() + timedelta(hours=1)).isoformat(),
        paid_at=now().isoformat(),
    )
    billing.prepare_payment(renewal["id"], provider)
    billing.poll_known_payments()
    assert billing.reconcile_inbox(provider) == 1
    assert provisioning.run_jobs() == 1
    with pytest.raises(DomainError):
        accounts.confirm_telegram_link(str(target_id), proof)
    RuntimePolicy.for_settings(config).admit(db)
    panel.client.close()
    provider.client.close()


@pytest.mark.parametrize(
    "tamper", ["extra_alias", "legacy_metadata", "source_marker", "audit", "current_identity"]
)
def test_scoped_merged_provenance_rejects_forged_expansion_and_restart(merge_context, tamper):
    from sunrise_infrastructure.db import Audit

    db, config, policy, accounts, token, auth, target_id = merge_context
    source_id = int(auth["account"]["id"])
    _, login = accounts.session(token)
    proof = accounts.request_link(
        str(source_id),
        policy.scope.telegram_id,
        login.email_confirmed_at,
        session_id=login.id,
        ip="127.0.0.1",
    )["proof"]
    accounts.confirm_telegram_link(str(target_id), proof)
    with db.transaction() as session:
        account = session.get(Account, source_id)
        marker = session.get(Setting, "scoped_test_account:" + str(source_id))
        if tamper == "extra_alias":
            session.add(Alias(old_id=999999, canonical_id=source_id))
        elif tamper == "legacy_metadata":
            account.legacy_consent = {"production_access": {"active": True}}
        elif tamper == "source_marker":
            value = dict(marker.value)
            sources = [dict(x) for x in value["sources"]]
            sources[0]["marker"] = {"origin": "imported"}
            marker.value = value | {"sources": sources}
        elif tamper == "audit":
            session.get(Audit, marker.value["audit_id"]).details = {"canonical_id": str(source_id)}
        else:
            account.telegram_id = 999999
    with pytest.raises(DomainError):
        accounts.session(token)
    with pytest.raises(DomainError):
        RuntimePolicy.for_settings(config).admit(db)


@pytest.mark.parametrize("bad", ["unmarked", "imported", "forged_alias"])
def test_scoped_merge_rejects_unapproved_identity_before_mutation(merge_context, bad):
    db, config, policy, accounts, token, auth, target_id = merge_context
    source_id = int(auth["account"]["id"])
    _, login = accounts.session(token)
    proof = accounts.request_link(
        str(source_id),
        policy.scope.telegram_id,
        login.email_confirmed_at,
        session_id=login.id,
        ip="127.0.0.1",
    )["proof"]
    with db.transaction() as session:
        if bad == "unmarked":
            session.delete(session.get(Setting, "scoped_test_account:" + str(target_id)))
        elif bad == "imported":
            session.get(Account, target_id).legacy_consent = {"source": "imported"}
        else:
            session.add(Alias(old_id=999999, canonical_id=target_id))
    with pytest.raises(DomainError):
        accounts.confirm_telegram_link(str(target_id), proof)
    with db.transaction() as session:
        assert session.get(Account, source_id).telegram_id is None
        assert session.get(Account, target_id).telegram_id == int(policy.scope.telegram_id)
        assert session.get(Alias, target_id) is None
