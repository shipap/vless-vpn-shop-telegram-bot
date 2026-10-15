import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin


@pytest.mark.parametrize("key", ["scoped_test_admission", "scoped_test_account:123", "promo_archive"])
def test_generic_admin_write_cannot_replace_scoped_identity_or_archived_promo_proof(key):
    service = Admin.__new__(Admin)
    with pytest.raises(DomainError) as error:
        service._save_setting("123", key, {}, "synthetic change")
    assert error.value.code == "deployment_settings_denied"
