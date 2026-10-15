"""Operational alert text explains the snapshot's record counts and environment."""

import pytest


@pytest.mark.parametrize("local_test", [True, False])
def test_alert_body_explains_each_record_category_without_claiming_unique_failed_orders(local_test):
    from sunrise_infrastructure.operational_monitor import operational_alert_body

    body = operational_alert_body(
        {
            "signal_count": 286,
            "counts": {
                "job": 219,
                "order": 65,
                "payment_event": 1,
                "provider_operation": 1,
                "delivery": 0,
            },
        },
        local_test=local_test,
    )
    assert "Записей, требующих внимания: 286." in body
    assert "Фоновые задачи: 219" in body
    assert "Заказы: 65" in body
    assert "Платёжные события: 1" in body
    assert "Операции провайдера: 1" in body
    assert "Доставка уведомлений: 0" in body
    assert "Счётчики показывают количество записей." in body
    assert "Один заказ может учитываться в нескольких категориях" in body
    assert ("Тестовый стенд" in body) is local_test
    assert "Повтор выдачи доступа не выполняет повторное списание денег." in body
