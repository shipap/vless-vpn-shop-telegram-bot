import json

import pytest
from sunrise_infrastructure.instance_config import InstanceConfig, load_instance_config
from sunrise_infrastructure.settings import Settings

from tests.e2e.local_auth_server import SyntheticSettings


def settings(**overrides):
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        secret_key="synthetic-config-settings-key-" * 2,
        local_test=True,
        instance_config_path=None,
        **overrides,
    )


def test_instance_alias_precedence_preserves_explicit_settings_and_synthetic_isolation(monkeypatch):
    monkeypatch.setenv("APP_NAME", "Public Environment")
    monkeypatch.setenv("SUNRISE_APP_NAME", "Legacy Environment")
    monkeypatch.setenv("DASHBOARD_URL", "http://127.0.0.1:4200")
    assert settings().instance.app_name == "Public Environment"
    configured = settings(app_name="Explicit Brand", dashboard_url="http://127.0.0.1:3200")
    assert configured.instance.app_name == "Explicit Brand"
    assert configured.instance.dashboard_url == "http://127.0.0.1:3200"
    synthetic = SyntheticSettings(
        _env_file=None,
        database_url=configured.database_url,
        secret_key=configured.secret_key,
        local_test=True,
        instance_config_path=None,
        app_name="Synthetic Brand",
        dashboard_url="http://127.0.0.1:3210",
    )
    assert synthetic.instance.app_name == "Synthetic Brand"
    assert synthetic.instance.dashboard_url == "http://127.0.0.1:3210"


def test_json_branding_and_legacy_links_resolve_from_one_instance(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/instance.json").write_text(
        json.dumps({"app_name": "Другой VPN", "dashboard_url": "https://dashboard.example.test"}),
        encoding="utf-8",
    )
    result = load_instance_config(project_root=tmp_path, overrides={"app_name": "Explicit Brand"})
    assert result.app_name == "Explicit Brand"
    assert result.app_short_name == result.sender_name == "Explicit Brand"
    assert result.dashboard_url == "https://dashboard.example.test"
    config = settings(bot_url="https://t.me/another_test_bot", landing_url="https://example.test")
    assert config.instance.telegram_bot_username == "another_test_bot"
    assert config.bot_url == config.instance.bot_url
    assert config.landing_url == config.instance.public_site_url


def test_empty_optional_alias_uses_neutral_defaults(monkeypatch):
    monkeypatch.setenv("APP_NAME", "")
    monkeypatch.setenv("SUPPORT_URL", "")
    assert settings().instance.app_name == "VLESS VPN Shop"
    assert settings().instance.support_url is None


def test_subscription_public_origin_alias_is_resolved_at_typed_instance_boundary(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_PUBLIC_ORIGIN", "https://connect.synthetic.invalid")
    configured = settings()
    assert configured.instance.subscription_public_origin == "https://connect.synthetic.invalid"
    assert configured.subscription_public_origin == configured.instance.subscription_public_origin


@pytest.mark.parametrize(
    "values",
    [
        {"app_name": "Unsafe\r\nInjected"},
        {"app_slug": "../other"},
        {"accent_color": "url(https://example.test)"},
        {"dashboard_url": "https://user:password@example.test"},
        {"dashboard_url": "http://example.test"},
        {"support_url": "javascript:alert(1)"},
        {"support_email": "other@example.test\r\nInjected"},
        {"telegram_bot_username": "name?start=unsafe"},
        {"telegram_menu_emoji_key": "../private"},
        {"telegram_menu_emoji_key": "a" * 49},
        {"logo_asset": "../../private.txt"},
        {"asset_root": "config"},
        {"database_url": "synthetic-private-marker"},
    ],
)
def test_instance_rejects_unsafe_values_without_disclosing_input(values):
    with pytest.raises(ValueError) as failure:
        InstanceConfig.model_validate(values)
    assert "synthetic-private-marker" not in str(failure.value)
    assert "password@example.test" not in str(failure.value)


def test_json_cannot_smuggle_transport_authorization(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/instance.json").write_text('{"panel_test_authorized":true}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_instance_config(project_root=tmp_path)


@pytest.mark.parametrize("path", ["../private.json", "/private.json", "config/../private.json"])
def test_config_loader_never_reads_outside_config_directory(tmp_path, path):
    with pytest.raises(ValueError, match="instance_config_path_invalid"):
        load_instance_config(path, project_root=tmp_path)


def test_branding_never_changes_exact_database_guard(monkeypatch):
    monkeypatch.setenv("APP_SLUG", "different-project")
    with pytest.raises(ValueError, match="55439|синтетическую"):
        Settings(
            _env_file=None,
            instance_config_path=None,
            database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/different_project",
            secret_key="synthetic-config-settings-key-" * 2,
        )


def test_init_only_synthetic_settings_ignore_real_private_json_and_ambient_config(tmp_path, monkeypatch):
    from sunrise_infrastructure import instance_config

    monkeypatch.setattr(instance_config, "PROJECT_ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/instance.json").write_text('{"app_name":"Private JSON Brand"}', encoding="utf-8")
    monkeypatch.setenv("APP_NAME", "Private Environment Brand")
    monkeypatch.setenv("INSTANCE_CONFIG_PATH", "config/instance.json")
    configured = SyntheticSettings(
        _env_file=None,
        database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        secret_key="synthetic-config-settings-key-" * 2,
        local_test=True,
    )
    assert configured.instance.app_name == "VLESS VPN Shop"
    assert configured.instance.visual_profile == "neutral"


def test_empty_config_path_disables_private_json(monkeypatch, tmp_path):
    from sunrise_infrastructure import instance_config

    monkeypatch.setattr(instance_config, "PROJECT_ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/instance.json").write_text('{"app_name":"Private JSON Brand"}', encoding="utf-8")
    configured = SyntheticSettings(
        _env_file=None,
        instance_config_path="",
        database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        secret_key="synthetic-config-settings-key-" * 2,
        local_test=True,
    )
    assert configured.instance.app_name == "VLESS VPN Shop"


def test_explicit_legacy_bot_url_wins_new_username_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "environment_bot")
    configured = settings(bot_url="https://t.me/explicit_bot")
    assert configured.instance.telegram_bot_username == "explicit_bot"
    assert configured.bot_url == "https://t.me/explicit_bot"


def test_duplicate_json_keys_cannot_select_an_ambiguous_brand(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/instance.json").write_text(
        '{"app_name":"First","app_name":"Second"}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="instance_config_invalid"):
        load_instance_config(project_root=tmp_path)


def test_optional_menu_emoji_key_is_explicit_and_never_part_of_public_identity():
    configured = InstanceConfig.model_validate({"telegram_menu_emoji_key": "legacy_private"})
    assert configured.telegram_menu_emoji_key == "legacy_private"
    assert "telegram_menu_emoji_key" not in configured.public()
    assert InstanceConfig().telegram_menu_emoji_key is None


def test_menu_emoji_alias_precedence_remains_presentation_only(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MENU_EMOJI_KEY", "public_registry")
    monkeypatch.setenv("SUNRISE_TELEGRAM_MENU_EMOJI_KEY", "legacy_registry")
    configured = settings(admin_telegram_allowlist=[], emoji_test_authorized=False)
    assert configured.instance.telegram_menu_emoji_key == "public_registry"
    assert configured.admin_telegram_allowlist == []
    assert configured.emoji_test_authorized is False
    assert (
        settings(telegram_menu_emoji_key="explicit_registry").instance.telegram_menu_emoji_key
        == "explicit_registry"
    )
