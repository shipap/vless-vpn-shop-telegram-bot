from decimal import Decimal

import pytest
from sunrise_infrastructure.legacy import normalize_legacy_money, resolve_squad


def test_float_conversion_records_difference_without_binary_artifact():
    value, delta = normalize_legacy_money("10.123456789")
    assert value == Decimal("10.123457")
    assert delta == Decimal("0.000000211")


def test_json_squad_preserved_and_conflict_not_silently_replaced():
    assert resolve_squad(None, '{"squad_uuid":"synthetic-squad"}') == "synthetic-squad"
    with pytest.raises(ValueError):
        resolve_squad("one", '{"squad_uuid":"two"}')
