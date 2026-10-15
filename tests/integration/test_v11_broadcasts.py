import importlib.util

import pytest
from sqlalchemy import select
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Notification, NotificationDelivery
from sunrise_infrastructure.instance_config import InstanceConfig
from test_v11_notifications import notification_context as notification_context


@pytest.mark.parametrize("app_name", ["VPN Shop", "Северный VPN", "Свой сервис с длинным названием"])
def test_broadcast_current_consent_and_one_channel(notification_context, app_name):
    db, settings, (owner, email, linked) = notification_context
    settings.instance = InstanceConfig(app_name=app_name)
    commerce = Commerce(db, local_test=True, public_settings=settings)
    documents = [
        {"document_type": k, "version": "v11", "url": f"https://example.test/{k}"}
        for k in ("offer", "privacy")
    ]
    commerce.legal.configure(None, documents, "Синтетические документы рассылки")
    with db.transaction() as session:
        for identifier in (owner, email):
            commerce.legal.accept_in(
                session, session.get(Account, identifier), documents, "website", "registration"
            )
    assert importlib.util.find_spec("sunrise_infrastructure.broadcasts") is not None, (
        "Unified broadcast missing"
    )
    from sunrise_infrastructure.broadcasts import BroadcastService

    service = BroadcastService(commerce, settings)
    service.create(
        owner, [{"type": "text", "text": "Синтетическая рассылка"}], "Проверка маршрутизации", draft=False
    )
    service.expand_pending()
    with db.transaction() as session:
        rows = list(session.scalars(select(Notification).where(Notification.kind == "broadcast")))
        assert {r.account_id for r in rows} == {owner, email}
        assert linked not in {r.account_id for r in rows}
        assert {r.title for r in rows} == {"Сообщение " + app_name}
        assert sorted(session.scalars(select(NotificationDelivery.channel))) == ["email", "telegram"]
    # A document version change makes prior consent insufficient for new deliveries.
    commerce.legal.configure(
        None, [{**d, "version": "v12"} for d in documents], "Обновление синтетических документов"
    )
    assert service.preview(owner, [{"type": "text", "text": "Проверка"}])["eligible_total"] == 0
