from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.catalog import validate_public_content
from sunrise_infrastructure.instance_config import InstanceConfig
from sunrise_infrastructure.service_content import help_center


def test_help_has_no_private_channels_without_admin_content():
    result = help_center(SimpleNamespace(), {}, {}, [])
    assert result["promo_channels"] == []
    assert "sunrise" not in str(result).lower()
    assert "15%" not in str(result)


def test_help_uses_admin_channels_and_arbitrary_periods():
    channels = [{"title": "Новости", "url": "https://news.example.test/codes"}]
    result = help_center(
        SimpleNamespace(),
        {"promo_channels": channels},
        {"referral_percent": "8"},
        [{"months": 7, "discount_percent": "13"}],
    )
    assert result["promo_channels"] == channels
    assert "7 мес. — 13%" in str(result)
    assert "8%" in str(result)


def test_admin_channels_are_validated_without_accepting_unsafe_links():
    content = {
        "faq": [],
        "how_to_connect": [],
        "promo_channels": [
            {"title": "Новости", "url": "https://news.example.test/codes"},
        ],
    }
    assert validate_public_content(content) == content
    content["promo_channels"][0]["url"] = "javascript:alert(1)"
    with pytest.raises(DomainError):
        validate_public_content(content)


def test_help_connection_uses_typed_instance_client_phrase_preserving_owner_faq():
    faq = [{"id": "owner", "question": "Собственный вопрос VPN", "answer": []}]
    settings = SimpleNamespace(
        instance=InstanceConfig(
            terminology={
                "client_label": "приложение",
                "client_installation_step": "Установите совместимое приложение, например Incy или Happ.",
            }
        )
    )
    result = help_center(settings, {"faq": faq}, {}, [])
    assert "Установите совместимое приложение, например Incy или Happ." in result["connection_instructions"]
    assert "VPN" not in result["connection_instructions"]
    assert result["custom_faq"] == faq
