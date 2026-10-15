"""Public distribution guards preserve examples while excluding private artifacts."""

import json
import subprocess
from pathlib import Path

from scripts.check_architecture import git_command
from scripts.setup_local_docker import check_ignored

ROOT = Path(__file__).resolve().parents[2]


def test_sensitive_filenames_are_ignored_but_public_sources_remain_available():
    excluded = [
        ".env", ".env.production", "nested/.env", "config/instance.local.yaml",
        "config/instance.yml", "certificate.pem", "tls/private.key", "certificate.p12",
        "local.sqlite3", "nested/local.db-wal", "backup.sql", "export.sql.gz",
        "backups/anything", "dumps/anything", "logs/server.txt", "runtime/session.json",
        "customer-data/export.csv", "deployment/local-compose.yml", ".private/backup.sql",
        ".ssh/id_ed25519", "nested/id_rsa", "secrets/api.json",
    ]
    included = [
        ".env.example", "config/instance.example.json", "requirements.lock", "package-lock.json",
        "migrations/versions/0020_example.sql", "tests/fixtures/schema.sql",
        "packages/core/src/sunrise_core/pricing.py", "docs/assets/screenshots/landing-mobile.png",
    ]
    with git_command(ROOT) as command:
        for path in excluded + included:
            result = subprocess.run(  # noqa: S603 -- local resolved Git, fixed read-only arguments
                [*command, "check-ignore", "--quiet", "--no-index", path], cwd=ROOT, capture_output=True,
            )
            assert result.returncode == (0 if path in excluded else 1), path


def test_default_instance_is_neutral_and_has_no_bundled_private_assets():
    example = json.loads((ROOT / "config/instance.example.json").read_text(encoding="utf-8"))
    assert example["app_name"] == example["app_short_name"] == "VLESS VPN Shop"
    assert example["visual_profile"] == "neutral"
    assert all(example[name] is None for name in ("logo_asset", "hero_asset", "favicon_asset", "og_image_asset"))
    contract = json.loads((ROOT / "contracts/openapi.json").read_text(encoding="utf-8"))
    assert contract["info"]["title"] == example["app_name"]


def test_shipped_ignore_rules_preserve_the_production_secret_guard():
    check_ignored(ROOT, env_file=".env.production", include_private=True)
