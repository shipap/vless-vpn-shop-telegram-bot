import httpx
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.providers import RollyPay


def provider_for(payload):
    return RollyPay(
        "https://payments.example.test",
        "synthetic",
        "terminal",
        {"payments.example.test"},
        recurring_authorized=True,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )


def test_documented_array_history_is_available_to_recurring_reconciliation():
    history = [{"cycle_number": 1, "status": "fail", "validation_status": "accepted"}]
    provider = provider_for(history)
    try:
        assert provider.charges("synthetic-agreement") == history
    finally:
        provider.client.close()


@pytest.mark.parametrize("payload", [None, {}, [None], ["fail"]])
def test_malformed_charge_history_never_becomes_confirmed_decline(payload):
    provider = provider_for(payload)
    try:
        with pytest.raises(DomainError) as error:
            provider.charges("synthetic-agreement")
        assert error.value.code == "provider_outcome_unknown"
    finally:
        provider.client.close()


def test_array_response_remains_invalid_for_payment_fact():
    provider = provider_for([])
    try:
        with pytest.raises(DomainError):
            provider.payment("synthetic-payment")
    finally:
        provider.client.close()
