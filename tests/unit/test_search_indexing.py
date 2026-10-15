import pytest
from sunrise_infrastructure.instance_config import InstanceConfig

from tests.unit.test_instance_config import settings


def test_public_instance_defaults_to_search_indexing_disabled():
    assert InstanceConfig().public()["search_indexing_enabled"] is False


def test_operator_can_explicitly_configure_indexing_without_changing_brand():
    instance = InstanceConfig(search_indexing_enabled=True, public_site_url="https://vpn.operator.net")
    assert instance.public()["search_indexing_enabled"] is True


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_instance_json_requires_an_actual_boolean_for_indexing(value):
    with pytest.raises(ValueError):
        InstanceConfig(search_indexing_enabled=value)


def test_environment_indexing_override_uses_the_same_typed_instance(monkeypatch):
    monkeypatch.setenv("SEARCH_INDEXING_ENABLED", "true")
    assert settings().instance.public()["search_indexing_enabled"] is True
    assert settings(search_indexing_enabled=False).instance.public()["search_indexing_enabled"] is False
