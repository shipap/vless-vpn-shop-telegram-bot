"""Preserve explicit legacy panel NULL without granting unbounded new purchases."""

import importlib.util
from pathlib import Path

import pytest
from sunrise_infrastructure.db import Segment, Subscription

ROOT = Path(__file__).resolve().parents[2]


def test_current_legacy_capacity_can_be_null_but_new_paid_segments_stay_finite():
    assert Subscription.__table__.c.devices_limit.nullable is True
    assert Segment.__table__.c.devices.nullable is False


def test_capacity_migration_widens_existing_column_and_refuses_destructive_revert():
    path = ROOT / "migrations/versions/0018_nullable_legacy_capacity.py"
    spec = importlib.util.spec_from_file_location("legacy_capacity_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "v11_0018" and module.down_revision == "v11_0017"
    with pytest.raises(RuntimeError, match="исторический"):
        module.downgrade()
