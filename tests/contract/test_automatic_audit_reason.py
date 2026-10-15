"""Interactive clients omit explanations while mutations retain a server audit reason."""

import pytest
from sunrise_api.schemas import PaymentMethodsUpdate, ProductUpdate, Reason
from sunrise_api.v11_routes import CampaignCreate, ConfirmedOperation, IdentityReason


@pytest.mark.parametrize(
    "model,values",
    [
        (Reason, {"confirmed": True}),
        (PaymentMethodsUpdate, {"confirmed": True, "policy": {}}),
        (IdentityReason, {}),
        (ConfirmedOperation, {"confirmed": True, "idempotency_key": "owned-operation"}),
        (
            CampaignCreate,
            {"confirmed": True, "name": "Название", "source": "site", "medium": "link", "campaign": "spring"},
        ),
        (ProductUpdate, {"confirmed": True, "price": "10.00"}),
    ],
)
def test_admin_actions_have_nonempty_automatic_audit_reason(model, values):
    body = model(**values)
    assert isinstance(body.reason, str) and 5 <= len(body.reason) <= 500


def test_confirmation_remains_required_after_reason_becomes_automatic():
    with pytest.raises(ValueError):
        Reason()
