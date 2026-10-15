from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure import mail


class SyntheticPolicy:
    production = True

    def __init__(self):
        self.scope = SimpleNamespace(
            smtp=SimpleNamespace(
                host="smtp.synthetic.test", port=587, mode="starttls", sender="shop@synthetic.test"
            )
        )
        self.allowed = True
        self.calls = []

    def require(self, capability=None):
        self.calls.append(capability)
        if not self.allowed:
            raise DomainError("production_stage_denied", "Операция недоступна.", 503)

    def require_current(self, session, capability=None):
        return self.require(capability)


class SyntheticSMTP:
    def __init__(self, on_login=lambda: None):
        self.on_login, self.messages = on_login, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ehlo(self):
        return None

    def starttls(self, **_):
        return None

    def login(self, *_):
        self.on_login()

    def send_message(self, message, **_):
        self.messages.append(message)
        return {}


@pytest.mark.parametrize("capability", ["auth_mail", "notice_email"])
def test_live_smtp_requires_trusted_fresh_callback_and_revocation_before_data(capability):
    assert hasattr(mail, "SMTPProductionMail"), "Production SMTP path is missing"
    policy = SyntheticPolicy()
    smtp = SyntheticSMTP()
    transport = mail.SMTPProductionMail(
        runtime_policy=policy,
        username="synthetic",
        password="synthetic-password",  # noqa: S106 -- synthetic SMTP only
        factory=lambda **_: smtp,  # noqa: S106 -- synthetic SMTP only
    )  # noqa: S106
    with pytest.raises(DomainError):
        transport.deliver("normal@synthetic.test", "Код", "123456", "challenge")
    assert not smtp.messages
    validations = []
    transport.deliver(
        "normal@synthetic.test",
        "Код",
        "123456",
        "challenge",
        capability=capability,
        before_send=lambda: validations.append("current"),
    )
    assert len(smtp.messages) == 1 and validations == ["current"]
    smtp.on_login = lambda: setattr(policy, "allowed", False)
    with pytest.raises(DomainError):
        transport.deliver(
            "normal@synthetic.test",
            "Код",
            "123456",
            "challenge-two",
            capability=capability,
            before_send=lambda: validations.append("current"),
        )
    assert len(smtp.messages) == 1


def test_project_panel_binding_preserves_original_uuid_and_gate_after_get():
    import httpx
    from sunrise_infrastructure import panel_v3

    assert hasattr(panel_v3, "RemnawaveProduction"), "Production panel adapter is missing"
    policy = SyntheticPolicy()
    policy.scope.scope_id = "synthetic-project"
    policy.scope.panel = SimpleNamespace(
        origin="https://panel.synthetic.test",
        contract="3.4_id",
        internal_squads=["11111111-1111-4111-8111-111111111111"],
        external_squads=[],
        subscription_hosts=["sub.synthetic.test"],
        node_uuids=[],
    )
    binding = {
        "scope_id": "synthetic-project",
        "generation": 1,
        "origin_nonce": "a" * 32,
        "user_id": "7",
        "uuid": "22222222-2222-4222-8222-222222222222",
        "username": "existing_customer",
        "short_uuid": "syntheticshortuuid123",
    }
    calls = []

    def handle(request):
        calls.append(request.method)
        policy.allowed = False
        return httpx.Response(
            200,
            json={
                "response": {
                    "id": 7,
                    "username": binding["username"],
                    "shortUuid": binding["short_uuid"],
                    "subscriptionUrl": "https://sub.synthetic.test/" + binding["short_uuid"],
                    "expireAt": "2030-01-01T00:00:00Z",
                }
            },
        )

    provider = panel_v3.RemnawaveProduction(
        runtime_policy=policy,
        token="synthetic-panel-token",  # noqa: S106 -- synthetic HTTP only
        transport=httpx.MockTransport(handle),  # noqa: S106 -- synthetic HTTP only
    )  # noqa: S106 -- mock transport only
    provider.register_project_binding(
        binding, connection_url="https://sub.synthetic.test/" + binding["short_uuid"]
    )
    current = {
        "id": 7,
        "username": binding["username"],
        "shortUuid": binding["short_uuid"],
        "subscriptionUrl": "https://sub.synthetic.test/" + binding["short_uuid"],
    }
    assert provider._connection(current, binding)["uuid"] == binding["uuid"]
    target = {
        "expires_at": "2030-02-01T00:00:00Z",
        "devices_limit": 1,
        "internal_squad_uuid": policy.scope.panel.internal_squads[0],
        "external_squad_uuid": None,
        "panel_object_type": "internal_squad",
    }
    with pytest.raises(DomainError):
        provider.apply(binding, target, before_mutation=lambda: None)
    assert calls == ["GET"]


def test_actual_identity_challenge_can_send_to_unverified_new_email_and_revoke_after_login():
    from contextlib import contextmanager
    from datetime import timedelta

    import fakeredis
    from sunrise_infrastructure.account_features import AccountsFeatures
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.db import Account, AccountIdentityOperation, Challenge, Session, Setting, now
    from sunrise_infrastructure.security import RateGuard, digest

    policy = SyntheticPolicy()
    policy.scope.scope_id = "synthetic-project"
    config = SimpleNamespace(
        runtime_mode="production",
        _runtime_policy=policy,
        secret_key="synthetic-purpose-bound-key-1234567890",  # noqa: S106 -- synthetic OTP only
    )  # noqa: S106 -- synthetic challenge proof only
    rows = {}

    def add(row):
        rows[(type(row), row.key if isinstance(row, Setting) else row.id)] = row

    tx = SimpleNamespace(
        add=add, get=lambda model, key, **_: rows.get((model, key)), scalar=lambda *args: None
    )

    @contextmanager
    def transaction():
        yield tx

    smtp = SyntheticSMTP()
    transport = mail.SMTPProductionMail(
        runtime_policy=policy,
        username="synthetic",
        password="synthetic-password",  # noqa: S106 -- synthetic SMTP only
        factory=lambda **_: smtp,  # noqa: S106 -- synthetic SMTP only
    )  # noqa: S106
    accounts = Accounts(
        SimpleNamespace(transaction=transaction),
        RateGuard(fakeredis.FakeRedis()),
        config,
        mail_transport=transport,
    )
    operation = AccountIdentityOperation(
        id="synthetic-operation",
        account_id=9007199254740993,
        kind="email_add",
        session_id="synthetic-session",
        state="new_email",
        new_email="new@synthetic.test",
        old_email=None,
        old_telegram_id=123,
        expires_at=now() + timedelta(minutes=10),
        payload={"ip_digest": digest("127.0.0.1")},
    )
    add(operation)
    add(Account(id=operation.account_id, email=None, email_verified=False, telegram_id=123))
    add(
        Session(
            id=operation.session_id,
            account_id=operation.account_id,
            expires_at=now() + timedelta(minutes=10),
            ip_address="127.0.0.1",
        )
    )
    cid, code, email = AccountsFeatures(accounts)._challenge_in(
        tx, operation, "new@synthetic.test", "127.0.0.1"
    )
    accounts._deliver_code(cid, code, email, "identity")
    assert len(smtp.messages) == 1
    evidence = rows[(Setting, "auth_mail:" + cid)].value
    assert evidence["purpose"] == "identity"
    assert evidence["identity_operation"] == operation.id
    challenge = rows[(Challenge, cid)]
    smtp.on_login = lambda: setattr(challenge, "used", True)
    with pytest.raises(DomainError, match="действителен"):
        accounts._deliver_code(cid, code, email, "identity")
    assert len(smtp.messages) == 1


def test_actual_link_otp_session_revoked_during_smtp_login_sends_no_data():
    from contextlib import contextmanager
    from datetime import timedelta

    import fakeredis
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.db import Account, Challenge, Session, Setting, now
    from sunrise_infrastructure.security import RateGuard

    policy = SyntheticPolicy()
    policy.scope.scope_id = "synthetic-project"
    config = SimpleNamespace(
        runtime_mode="production",
        _runtime_policy=policy,
        secret_key="synthetic-link-proof-key-1234567890",  # noqa: S106 -- synthetic OTP only
    )  # noqa: S106
    actor = Account(
        id=9007199254740993,
        email="current@synthetic.test",
        email_verified=True,
        legacy_consent={
            "production_access": {
                "version": 1,
                "account_id": "9007199254740993",
                "state": "active",
                "source": "legacy_user_is_active",
                "source_account_id": "9007199254740993",
            }
        },
    )
    login = Session(id="synthetic-session", account_id=actor.id, expires_at=now() + timedelta(minutes=10))
    rows = {(Account, actor.id): actor, (Session, login.id): login}

    def add(row):
        rows[(type(row), row.key if isinstance(row, Setting) else row.id)] = row

    tx = SimpleNamespace(
        add=add,
        get=lambda model, key, **_: rows.get((model, key)),
        execute=lambda *args: None,
        scalar=lambda query, *args: (
            actor if getattr(query, "column_descriptions", [{}])[0].get("entity") is Account else None
        ),
    )

    @contextmanager
    def transaction():
        yield tx

    smtp = SyntheticSMTP(on_login=lambda: setattr(login, "expires_at", now() - timedelta(seconds=1)))
    transport = mail.SMTPProductionMail(
        runtime_policy=policy,
        username="synthetic",
        password="synthetic-password",  # noqa: S106 -- synthetic SMTP only
        factory=lambda **_: smtp,  # noqa: S106 -- synthetic SMTP only
    )  # noqa: S106
    service = Accounts(
        SimpleNamespace(transaction=transaction),
        RateGuard(fakeredis.FakeRedis()),
        config,
        mail_transport=transport,
    )
    with pytest.raises(DomainError):
        service.request_link_email(actor.id, login.id, "127.0.0.1")
    assert smtp.messages == []
    assert len([key for key in rows if key[0] is Challenge]) == 1


def test_imported_explicit_null_renewal_omits_capacity_until_finite_paid_boundary(monkeypatch):
    import inspect
    import json
    from datetime import timedelta

    import httpx
    from sunrise_infrastructure import panel_v3
    from sunrise_infrastructure.db import now

    assert (
        "capacity_evidence"
        in inspect.signature(panel_v3.RemnawaveProduction.register_project_binding).parameters
    ), "Imported nullable capacity proof is missing"
    policy = SyntheticPolicy()
    policy.scope.scope_id = "synthetic-project"
    squad = "11111111-1111-4111-8111-111111111111"
    policy.scope.panel = SimpleNamespace(
        origin="https://panel.synthetic.test",
        contract="3.4_id",
        internal_squads=[squad],
        external_squads=[],
        subscription_hosts=["sub.synthetic.test"],
        node_uuids=[],
    )
    original_expiry = (now() + timedelta(days=2)).isoformat()
    renewed_expiry = (now() + timedelta(days=32)).isoformat()
    binding = {
        "scope_id": "synthetic-project",
        "generation": 1,
        "origin_nonce": "b" * 32,
        "user_id": "8",
        "uuid": "22222222-2222-4222-8222-222222222222",
        "username": "preserved_customer",
        "short_uuid": "syntheticshortuuid456",
    }
    evidence = {
        "kind": "explicit_null_preserved",
        "present": True,
        "source": "authenticated_panel_snapshot",
        "until": original_expiry,
    }
    remote = {
        "id": 8,
        "username": binding["username"],
        "shortUuid": binding["short_uuid"],
        "subscriptionUrl": "https://sub.synthetic.test/" + binding["short_uuid"],
        "hwidDeviceLimit": None,
        "activeInternalSquads": [{"uuid": squad}],
        "status": "ACTIVE",
        "expireAt": original_expiry,
    }
    patches = []

    def handle(request):
        if request.method == "PATCH":
            body = json.loads(request.content)
            patches.append(body)
            remote.update(body)
            remote["activeInternalSquads"] = [{"uuid": value} for value in body["activeInternalSquads"]]
        return httpx.Response(200, json={"response": remote.copy()})

    provider = panel_v3.RemnawaveProduction(
        runtime_policy=policy,
        token="synthetic-panel-token",  # noqa: S106 -- synthetic HTTP only
        transport=httpx.MockTransport(handle),  # noqa: S106 -- synthetic HTTP only
    )  # noqa: S106 -- synthetic mock token
    provider.register_project_binding(
        binding, connection_url=remote["subscriptionUrl"], capacity_evidence=evidence
    )
    target = {
        "expires_at": renewed_expiry,
        "devices_limit": None,
        "preserve_null_capacity": True,
        "capacity_evidence": evidence,
        "internal_squad_uuid": squad,
        "external_squad_uuid": None,
        "panel_object_type": "internal_squad",
    }
    result = provider.apply(binding, target, before_mutation=lambda: None)
    assert "hwidDeviceLimit" not in patches[0]
    assert result["applied_target"]["devices_limit"] is None
    assert result["uuid"] == binding["uuid"]
    from datetime import UTC, datetime

    class BoundaryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(original_expiry).astimezone(tz or UTC)

    with pytest.raises(panel_v3.PanelReview, match="preservation"):
        provider.apply(
            binding, target, before_mutation=lambda: monkeypatch.setattr(panel_v3, "datetime", BoundaryClock)
        )
    assert len(patches) == 1
    monkeypatch.setattr(panel_v3, "datetime", BoundaryClock)
    with pytest.raises(panel_v3.PanelReview, match="preservation"):
        provider.apply(binding, target, before_mutation=lambda: None)
    assert len(patches) == 1
    provider.apply(
        binding, target | {"devices_limit": 4, "preserve_null_capacity": False}, before_mutation=lambda: None
    )
    assert patches[1]["hwidDeviceLimit"] == 4 and len(patches) == 2


def test_rotation_revalidates_persisted_authority_after_final_policy_wait_before_post():
    import httpx
    from sunrise_infrastructure import panel_v3

    policy = SyntheticPolicy()
    policy.scope.scope_id = "synthetic-project"
    policy.scope.panel = SimpleNamespace(
        origin="https://panel.synthetic.test",
        contract="3.4_id",
        internal_squads=["11111111-1111-4111-8111-111111111111"],
        external_squads=[],
        subscription_hosts=["sub.synthetic.test"],
        node_uuids=[],
    )
    binding = {
        "scope_id": "synthetic-project",
        "generation": 1,
        "origin_nonce": "a" * 32,
        "user_id": "7",
        "uuid": "22222222-2222-4222-8222-222222222222",
        "username": "existing_customer",
        "short_uuid": "syntheticshortuuid123",
    }
    calls, proofs = [], []
    revoked = False

    def require(capability=None):
        nonlocal revoked
        if capability == "panel_mutation" and proofs:
            revoked = True

    policy.require = require

    def before_rotate(proof):
        if revoked:
            raise panel_v3.PanelReview("panel_owner_changed")
        proofs.append(proof)

    def handle(request):
        calls.append(request.method)
        short = "rotatedsyntheticshort123" if "POST" in calls else binding["short_uuid"]
        return httpx.Response(
            200,
            json={
                "response": {
                    "id": 7,
                    "username": binding["username"],
                    "shortUuid": short,
                    "subscriptionUrl": "https://sub.synthetic.test/" + short,
                    "expireAt": "2030-01-01T00:00:00Z",
                }
            },
        )

    provider = panel_v3.RemnawaveProduction(
        runtime_policy=policy,
        token="synthetic-panel-token",  # noqa: S106 -- synthetic HTTP only
        transport=httpx.MockTransport(handle),  # noqa: S106 -- synthetic HTTP only
    )  # noqa: S106
    provider.register_project_binding(
        binding, connection_url="https://sub.synthetic.test/" + binding["short_uuid"]
    )
    with pytest.raises(panel_v3.PanelReview, match="owner_changed"):
        provider.rotate(binding, before_rotate=before_rotate)
    assert calls == ["GET"] and len(proofs) == 1
