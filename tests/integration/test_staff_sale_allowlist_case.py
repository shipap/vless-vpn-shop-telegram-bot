from sqlalchemy import select
from sunrise_infrastructure.db import Account, Notification

from tests.integration.test_staff_sale_notifications import fulfill
from tests.integration.test_staff_sale_notifications import sale_context as sale_context


def test_sale_recipients_use_telegram_authority_independently_of_email_allowlist(sale_context):
    commerce, config = sale_context
    config.admin_email_allowlist = []
    identifier = fulfill(commerce, "purchase")
    with commerce.db.transaction() as session:
        recipients = list(
            session.scalars(
                select(Notification.account_id).where(Notification.event_key == "sale:" + identifier)
            )
        )
    assert set(recipients) == {101, 102}


def test_sale_excludes_verified_email_only_staff_and_keeps_telegram_only_staff(sale_context):
    commerce, _ = sale_context
    with commerce.db.transaction() as session:
        for identifier in (101, 102):
            actor = session.get(Account, identifier)
            actor.email, actor.email_verified = None, False
        session.get(Account, 103).email_verified = True
    identifier = fulfill(commerce, "purchase")
    with commerce.db.transaction() as session:
        recipients = set(session.scalars(
            select(Notification.account_id).where(Notification.event_key == "sale:" + identifier)
        ))
    assert recipients == {101, 102}
