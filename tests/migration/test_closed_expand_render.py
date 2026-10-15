"""Offline release SQL uses the actual reviewed chain and preserves legacy tables."""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def renderer():
    path = ROOT / "scripts" / "render_closed_expand.py"
    spec = importlib.util.spec_from_file_location("closed_expand_renderer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_actual_expand_chain_is_deterministic_and_preserves_legacy_tables():
    module = renderer()
    first, proof = module.render(ROOT / "migrations" / "versions")
    second, second_proof = module.render(ROOT / "migrations" / "versions")
    assert first == second and proof == second_proof
    assert proof["revisions"][-1] == "v11_0019"
    assert len(proof["revisions"]) == 19
    assert "CREATE TABLE v5_accounts" in first
    assert "CREATE TABLE v5_alembic_version" in first
    assert "UPDATE v5_alembic_version" in first
    assert not re.search(r"%\([a-z_]+\)s|:[a-z_]+", first)
    for revision in proof["revisions"]:
        assert "'" + revision + "'" in first
    assert "BEGIN;" in first and first.rstrip().endswith("COMMIT;")
    assert "DROP TABLE" not in first.upper()
    for table in ("users", "subscriptions", "purchases", "items", "settings"):
        assert f"ALTER TABLE {table} " not in first
        assert f"UPDATE {table} " not in first
        assert f"DELETE FROM {table} " not in first  # noqa: S608 - literal SQL assertion, never executed


def test_incomplete_chain_is_rejected_before_emitting_sql(tmp_path):
    (tmp_path / "0001_bad.py").write_text(
        "revision='bad'\ndown_revision='missing'\ndef upgrade(): pass\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="chain"):
        renderer().render(tmp_path)


def test_rendering_does_not_load_database_environment(monkeypatch):
    monkeypatch.setenv("SUNRISE_DATABASE_URL", "postgresql://invalid/production")
    rendered, _ = renderer().render(ROOT / "migrations" / "versions")
    assert "invalid" not in rendered


def test_incremental_render_only_widens_capacity_and_advances_exact_revision():
    rendered, proof = renderer().render(ROOT / "migrations" / "versions", since_revision="v11_0017")
    assert proof["from_revision"] == "v11_0017"
    assert proof["revisions"] == ["v11_0018", "v11_0019"]
    assert "CREATE TABLE" not in rendered
    assert "ALTER TABLE v5_subscriptions ALTER COLUMN devices_limit DROP NOT NULL" in rendered
    assert "WHERE v5_alembic_version.version_num = 'v11_0017'" in rendered
    assert "'v11_0018'" in rendered
    assert "ALTER TABLE v9_device_unlinks ADD COLUMN unlink_mode" in rendered
    assert "'v11_0019'" in rendered


@pytest.mark.parametrize("revision", ["unknown", "v11_0019"])
def test_incremental_render_denies_unknown_or_already_current_revision(revision):
    with pytest.raises(ValueError, match="revision"):
        renderer().render(ROOT / "migrations" / "versions", since_revision=revision)
