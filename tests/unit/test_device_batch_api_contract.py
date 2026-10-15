"""The device API keeps an explicit bulk command when it contains one device."""

from sunrise_api.v11_routes import DeviceBatch


def test_one_device_bulk_mode_survives_api_validation():
    batch = DeviceBatch.model_validate(
        {
            "reason": "Подтверждена отвязка устройств",
            "confirmed": True,
            "idempotency_key": "bulk-one",
            "device_ids": ["device-1"],
            "operation_mode": "bulk",
        }
    )
    assert batch.operation_mode == "bulk"


def test_old_device_client_still_has_no_explicit_mode():
    batch = DeviceBatch.model_validate(
        {
            "reason": "Подтверждена отвязка устройств",
            "confirmed": True,
            "idempotency_key": "legacy-one",
            "device_ids": ["device-1"],
        }
    )
    assert batch.operation_mode is None
