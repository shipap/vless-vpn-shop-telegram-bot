import pytest
from pydantic import ValidationError
from sunrise_infrastructure.instance_config import InstanceConfig


def test_default_instance_preserves_generic_vpn_terminology():
    instance = InstanceConfig()
    assert instance.terminology.service_label == "VPN"
    assert instance.terminology.connect_label == "Подключить VPN"
    assert instance.public()["terminology"]["connections_label"] == "Мой VPN"


def test_private_terminology_is_typed_public_and_independent_of_visual_profile():
    instance = InstanceConfig(
        visual_profile="neutral",
        terminology={
            "service_label": "Защищённое подключение",
            "connect_label": "Настроить подключение",
            "connections_label": "Мои подключения",
            "hero_title": "Ваше защищённое подключение.",
        },
    )
    assert instance.public()["terminology"]["connect_label"] == "Настроить подключение"
    assert instance.terminology.client_label == "VPN-приложение"
    assert InstanceConfig(visual_profile="private").terminology.service_label == "VPN"


@pytest.mark.parametrize("value", ["", " ", "x" * 241, "line\ncontrol", "delete\x7fcontrol", 123, None])
def test_invalid_instance_ui_terms_are_rejected_without_echoing_input(value):
    with pytest.raises(ValidationError) as failure:
        InstanceConfig(terminology={"connect_label": value})
    assert "input_value" not in str(failure.value)


def test_business_policy_and_secrets_cannot_be_added_to_terminology():
    with pytest.raises(ValidationError):
        InstanceConfig(terminology={"price": "50", "api_token": "fixture-only"})


def test_complete_bot_phrases_are_configurable_without_grammar_replacement():
    instance = InstanceConfig(
        terminology={
            "welcome_body": "Подключение и платежи — в одном месте.",
            "provisioning_pending_body": "Готовим защищённое подключение.",
            "provisioning_ready_body": "Подключение готово.",
        }
    )
    assert instance.terminology.provisioning_pending_body == "Готовим защищённое подключение."
    assert InstanceConfig().terminology.welcome_body == "Ваш VPN, подписки и платежи — в одном месте."


def test_welcome_body_can_be_hidden_without_allowing_empty_action_labels():
    instance = InstanceConfig(terminology={"welcome_body": ""})
    assert instance.terminology.welcome_body == ""
    assert instance.public()["terminology"]["welcome_body"] == ""
    with pytest.raises(ValidationError):
        InstanceConfig(terminology={"connect_label": ""})


def test_installation_step_preserves_generic_copy_and_accepts_private_phrase():
    assert InstanceConfig().terminology.client_installation_step == (
        "Установите совместимый VPN-клиент, например Incy или Happ."
    )
    phrase = "Установите совместимое приложение, например Incy или Happ."
    assert (
        InstanceConfig(terminology={"client_installation_step": phrase}).terminology.client_installation_step
        == phrase
    )
