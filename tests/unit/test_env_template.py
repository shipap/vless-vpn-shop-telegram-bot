"""Private environment updates must preserve credentials and reject tracked files."""

import importlib.util
from pathlib import Path

import pytest
from dotenv import dotenv_values


def module():
    path = Path(__file__).resolve().parents[2] / "scripts/update_test_env.py"
    spec = importlib.util.spec_from_file_location("update_test_env", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_example_uses_public_instance_aliases_and_neutral_defaults(tmp_path):
    values = module().build_template(tmp_path)
    assert values["APP_NAME"] == "VLESS VPN Shop"
    assert "LOGO_ASSET_PATH" in values and "PUBLIC_SITE_URL" in values
    assert "SUNRISE_APP_NAME" not in values
    assert "SUNRISE_SMTP_SETUP_SSH_PASSWORD" not in values
    assert "bububi" not in (tmp_path / ".env.example").read_text(encoding="utf-8")


def test_merge_preserves_credentials_and_unknown_keys(tmp_path):
    template = tmp_path / ".env.example"
    target = tmp_path / ".env"
    template.write_text("# Настройки\nSECRET=\nNEW=false\n", encoding="utf-8")
    target.write_text("SECRET='private#value'\nOLD='retained'\n", encoding="utf-8")
    module().merge_template(template, target, {"NEW": "true"})
    values = dotenv_values(target)
    assert values == {"SECRET": "private#value", "NEW": "true", "OLD": "retained"}
    assert "private#value" not in template.read_text(encoding="utf-8")


def test_multiline_values_are_rejected_before_write(tmp_path):
    template = tmp_path / ".env.example"
    target = tmp_path / ".env"
    template.write_text("SECRET=\n", encoding="utf-8")
    with pytest.raises(ValueError):
        module().merge_template(template, target, {"SECRET": "first\nSECOND=secret"})
    assert not target.exists()


def test_compact_environment_keeps_secrets_and_removes_unused_defaults(tmp_path):
    target = tmp_path / ".env"
    target.write_text("SECRET='keep'\nFLAG=false\nHOST=''\nOLD_SECRET='also-keep'\n", encoding="utf-8")
    module().compact_environment(target, {"FLAG": "false", "HOST": ""}, {"HOST"}, {"HOST": "TEST host"})
    assert dotenv_values(target) == {"SECRET": "keep", "HOST": "", "OLD_SECRET": "also-keep"}


def test_compaction_cannot_overwrite_concurrent_owner_edit(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    target.write_text("SECRET='old'\n", encoding="utf-8")
    updater = module()
    original_quote = updater.quote

    def owner_edits_while_rendering(value):
        target.write_text("SECRET='owner-new'\n", encoding="utf-8")
        return original_quote(value)

    monkeypatch.setattr(updater, "quote", owner_edits_while_rendering)
    with pytest.raises(ValueError):
        updater.compact_environment(target, {}, set(), {})
    assert dotenv_values(target)["SECRET"] == "owner-new"  # noqa: S105 -- synthetic concurrency fixture
