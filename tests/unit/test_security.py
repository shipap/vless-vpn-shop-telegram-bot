import fakeredis
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.security import RateGuard


def test_redis_tenth_failure_blocks_context_and_cooldown_survives_new_object():
    r = fakeredis.FakeRedis()
    g = RateGuard(r)
    g.resend("email-hash")
    with pytest.raises(DomainError):
        RateGuard(r).resend("email-hash")
    for _ in range(9):
        assert not g.fail("context")
    assert g.fail("context")
    with pytest.raises(DomainError):
        g.check("context")
    assert 3500 <= r.ttl("sunrise:block:context") <= 3600


def test_unblock_only_specific_context_does_not_reset_resend():
    r = fakeredis.FakeRedis()
    g = RateGuard(r)
    g.resend("email-hash")
    for _ in range(10):
        g.fail("context")
    g.unblock("context")
    g.check("context")
    with pytest.raises(DomainError):
        g.resend("email-hash")


def test_scan_requires_both_count_and_distinct_paths():
    r = fakeredis.FakeRedis()
    g = RateGuard(r)
    for _ in range(70):
        assert not g.missing_route("scan", "/same")
    for i in range(18):
        assert not g.missing_route("scan", "/different" + str(i))
    assert g.missing_route("scan", "/last")
    with pytest.raises(DomainError):
        g.check("scan")


def test_bot_admin_uses_admitted_telegram_id_without_email_authority():
    from types import SimpleNamespace

    from sunrise_infrastructure.accounts import Accounts

    settings = SimpleNamespace(admin_email_allowlist=["admin@example.test"], admin_telegram_allowlist=["101"])
    accounts = Accounts(None, None, settings)
    actor = SimpleNamespace(telegram_id=101, email="admin@example.test", email_verified=False)
    assert accounts.is_bot_admin(actor)
    actor.email_verified = True
    assert accounts.is_bot_admin(actor)
    actor.email = "outsider@example.test"
    assert accounts.is_bot_admin(actor)
    actor.telegram_id = 999
    assert not accounts.is_bot_admin(actor)
