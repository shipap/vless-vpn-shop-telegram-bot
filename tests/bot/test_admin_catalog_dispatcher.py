"""Редактор старого товара через настоящий dispatcher и синтетическую БД."""

import asyncio

import pytest
from aiogram.types import MessageEntity
from sqlalchemy import func, select
from sunrise_infrastructure.db import Audit, Product
from test_admin_dispatcher import bind_console
from test_dispatcher import callback_update, find_button, message_update
from test_shared_backend import shared_backend  # noqa: F401 -- pytest fixture


@pytest.mark.parametrize(("field", "label"), [("name", "Название"), ("description", "Описание")])
def test_owner_edits_imported_product_with_premium_emoji_through_dispatcher(shared_backend, field, label):  # noqa: F811
    async def run():
        app, service, accounts, session, aid, telegram = shared_backend
        accounts.settings.owner_id = str(telegram)
        bind_console(app, service, accounts, telegram, verify_email=False)
        app.presenter.custom_emoji = True
        with service.db.transaction() as tx:
            product = tx.scalar(select(Product).order_by(Product.id))
            product.content = {"legacy_item_data_preserved_in": "synthetic-import"}
            pid, name, price, version = product.id, product.name, product.price, product.version
            audits = tx.scalar(select(func.count()).select_from(Audit))

        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
        for text in ["Админ-панель", "Товары", name, label]:
            await app.dispatcher.feed_update(
                app.bot, callback_update(find_button(session, text), uid=telegram)
            )
        update = message_update(uid=telegram, text="🚀 ⭐ Новый текст")
        update = update.model_copy(
            update={
                "message": update.message.model_copy(
                    update={
                        "entities": [
                            MessageEntity(
                                type="custom_emoji", offset=3, length=1, custom_emoji_id="9007199254740993"
                            )
                        ]
                    }
                )
            }
        )
        await app.dispatcher.feed_update(app.bot, update)
        assert '<tg-emoji emoji-id="9007199254740993">⭐</tg-emoji>' in session.calls[-1].text
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Сохранить"), uid=telegram)
        )
        with service.db.transaction() as tx:
            product = tx.get(Product, pid)
            assert getattr(product, field) == "🚀 ⭐ Новый текст"
            assert product.content["legacy_item_data_preserved_in"] == "synthetic-import"
            assert product.content[field][0]["children"][1]["id"] == "9007199254740993"
            assert product.price == price and product.version == version + 1
            assert tx.scalar(select(func.count()).select_from(Audit)) == audits + 1

    asyncio.run(run())
