import fakeredis
import pytest
import sunrise_api.main as api
import sunrise_infrastructure.providers as provider_module
import sunrise_worker.main as worker
from sunrise_infrastructure.settings import Settings


def settings(tmp_path, **overrides):
    values = {
        "database_url": "postgresql+psycopg://test@127.0.0.1:55439/sunrise_test",
        "secret_key": "x" * 32,
        "local_test": True,
        "mail_dir": str(tmp_path),
        "rollypay_api_key": "synthetic-api-key",
        "rollypay_signing_secret": "synthetic-signing-secret",
        "rollypay_terminal": "synthetic-terminal",
    }
    return Settings(_env_file=None, **(values | overrides))


def test_official_defaults_require_explicit_external_test_authorization(tmp_path):
    config = settings(tmp_path)
    assert config.rollypay_base_url == "https://rollypay.io"
    assert config.payment_allowed_hosts == ["rollypay.io"]
    assert config.rollypay_test_authorized is False


@pytest.mark.parametrize("base", [None, "", "   "])
def test_blank_base_normalizes_to_known_official_default(tmp_path, base):
    assert settings(tmp_path, rollypay_base_url=base).rollypay_base_url == "https://rollypay.io"


@pytest.mark.parametrize("module", [api, worker], ids=["api", "worker"])
def test_filled_credentials_never_construct_provider_without_authorization(tmp_path, monkeypatch, module):
    def forbidden(*args, **kwargs):
        pytest.fail("A stored credential must not enable external provider composition")

    monkeypatch.setattr(provider_module, "RollyPay", forbidden)
    config = settings(tmp_path, rollypay_base_url="https://rollypay.io", payment_allowed_hosts=[])
    if module is api:
        app = api.create_app(config, redis_client=fakeredis.FakeRedis())
        assert any(route.path == "/api/v1/webhooks/rollypay" for route in app.routes)
        app.state.commerce.db.engine.dispose()
    else:
        instance = worker.Worker(config, redis_client=fakeredis.FakeRedis())
        assert instance.provider is None
        instance.db.engine.dispose()


@pytest.mark.parametrize("module", [api, worker], ids=["api", "worker"])
def test_authorized_composition_uses_explicit_host_guard(tmp_path, monkeypatch, module):
    original = provider_module.RollyPay
    providers = []

    def capture(*args, **kwargs):
        provider = original(*args, **kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(provider_module, "RollyPay", capture)
    config = settings(tmp_path, rollypay_test_authorized=True)
    if module is api:
        instance = api.create_app(config, redis_client=fakeredis.FakeRedis())
        instance.state.commerce.db.engine.dispose()
    else:
        instance = worker.Worker(config, redis_client=fakeredis.FakeRedis())
        instance.db.engine.dispose()
    assert len(providers) == 1
    assert str(providers[0].client.base_url) == "https://rollypay.io"
    providers[0].client.close()


@pytest.mark.parametrize("module", [api, worker], ids=["api", "worker"])
def test_authorized_unlisted_host_is_denied(tmp_path, module):
    config = settings(tmp_path, rollypay_test_authorized=True, rollypay_base_url="https://unapproved.test")
    with pytest.raises(ValueError, match="адрес не разрешён"):
        if module is api:
            api.create_app(config, redis_client=fakeredis.FakeRedis())
        else:
            worker.Worker(config, redis_client=fakeredis.FakeRedis())


@pytest.mark.parametrize("module", [api, worker], ids=["api", "worker"])
def test_dependency_injection_remains_available_when_external_composition_is_off(
    tmp_path, monkeypatch, module
):
    injected = object()
    observed = []

    def forbidden(*args, **kwargs):
        pytest.fail("Explicit injected test providers must bypass network composition")

    monkeypatch.setattr(provider_module, "RollyPay", forbidden)
    config = settings(tmp_path)
    if module is api:
        monkeypatch.setattr(api, "Recurring", lambda commerce, provider: observed.append(provider))
        app = api.create_app(config, redis_client=fakeredis.FakeRedis(), payment_provider=injected)
        assert observed == [injected]
        app.state.commerce.db.engine.dispose()
    else:
        instance = worker.Worker(config, redis_client=fakeredis.FakeRedis(), provider=injected)
        assert instance.provider is injected
        instance.db.engine.dispose()


@pytest.mark.parametrize("module", [api, worker], ids=["api", "worker"])
@pytest.mark.parametrize("missing", ["rollypay_api_key", "rollypay_signing_secret", "rollypay_terminal"])
def test_authorized_incomplete_credentials_do_not_construct(tmp_path, monkeypatch, module, missing):
    def forbidden(*args, **kwargs):
        pytest.fail("Incomplete callback or API credentials must not enable outgoing payments")

    monkeypatch.setattr(provider_module, "RollyPay", forbidden)
    config = settings(tmp_path, **{"rollypay_test_authorized": True, missing: ""})
    if module is api:
        app = api.create_app(config, redis_client=fakeredis.FakeRedis())
        app.state.commerce.db.engine.dispose()
    else:
        instance = worker.Worker(config, redis_client=fakeredis.FakeRedis())
        assert instance.provider is None
        instance.db.engine.dispose()
