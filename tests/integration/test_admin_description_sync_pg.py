"""Presentation updates with real isolated PostgreSQL and existing staff/MFA gates."""

from copy import deepcopy

import pytest
from sqlalchemy import func, select
from sunrise_core.errors import DomainError
from sunrise_core.telegram_content import render_classic, telegram_html_to_content
from sunrise_infrastructure.db import Audit, CreditLedger, Ledger, Order, Payment, Product

from tests.integration.test_bot_admin_v7 import bot_admin_v7 as approved_admin_fixture

bot_admin_v7 = approved_admin_fixture


def create_product(facade, actor):
    return facade.bot_product_save(
        actor,
        None,
        {
            "code": "synthetic_owner_description",
            "name": "Синтетический тариф",
            "description": "Старое описание",
            "tier_level": 1,
            "price": "99.00",
            "currency": "RUB",
            "included_devices": 4,
            "available": True,
            "content": {
                "name": [{"type": "text", "text": "Название AST"}],
                "description": [{"type": "text", "text": "Старое описание"}],
            },
        },
        "Создание синтетического тарифа",
        confirmed=True,
    )


@pytest.mark.parametrize("resend_ast", [False, True])
def test_actual_plain_edit_updates_premium_selected_lot_and_preserves_money(bot_admin_v7, resend_ast):
    facade, actor, _ = bot_admin_v7
    old = create_product(facade, actor)
    raw = '<b>Новое описание</b> <tg-emoji emoji-id="9007199254740993">🌐</tg-emoji>'
    patch = {"description": raw}
    if resend_ast:
        patch["content"] = deepcopy(old["content"])
    facade.bot_product_save(actor, old["id"], patch, "Изменить описание тарифа", confirmed=True)
    current = next(
        product for product in facade.admin.commerce.catalog()["products"] if product["id"] == old["id"]
    )
    assert current["content"]["description"] == telegram_html_to_content(raw)
    assert current["content"]["name"] == old["content"]["name"]
    assert current["price"] == old["price"] and current["included_devices"] == old["included_devices"]
    assert 'emoji-id="9007199254740993"' in render_classic(
        current["content"]["description"], custom_emoji=True
    )
    with facade.admin.db.transaction() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Audit)
                .where(Audit.action == "catalog_update", Audit.target == old["id"])
            )
            == 1
        )


def test_actual_faq_update_keeps_restored_intro_and_shared_catalog_projection(bot_admin_v7):
    facade, actor, _ = bot_admin_v7
    nodes = telegram_html_to_content('<tg-emoji emoji-id="9007199254740993">🌐</tg-emoji> Каталог')
    facade.bot_catalog_intro_save(actor, nodes, "Восстановить отдельный каталог", confirmed=True)
    before = facade.admin.public_content()
    facade.admin.update_public_content(actor, {"faq": [], "how_to_connect": []}, "Обновить только FAQ")
    after = facade.admin.public_content()
    assert after["catalog_description_content"] == nodes
    assert after["catalog_description"] == before["catalog_description"]
    content = facade.admin.commerce.catalog()["content"]
    assert content["catalog_description_content"] == nodes


def test_actual_invalid_content_rollback_keeps_product_version_and_audit(bot_admin_v7):
    facade, actor, _ = bot_admin_v7
    old = create_product(facade, actor)
    with facade.admin.db.transaction() as session:
        count = session.scalar(select(func.count()).select_from(Audit))
    with pytest.raises(DomainError):
        facade.admin.catalog_update(
            actor, old["id"], {"description": "Новое", "content": []}, "Некорректный контент"
        )
    with facade.admin.db.transaction() as session:
        product = session.get(Product, old["id"])
        assert product.version == old["version"]
        assert product.description == old["description"] and product.content == old["content"]
        assert session.scalar(select(func.count()).select_from(Audit)) == count


@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize("content_mode", ["partial", "resend_old", "fresh_premium"])
def test_actual_legacy_catalog_text_edit_preserves_metadata_and_money(bot_admin_v7, field, content_mode):
    facade, actor, _ = bot_admin_v7
    old = create_product(facade, actor)
    metadata = {"source": "synthetic-private-record", "revision": 1}
    money_models = (Order, Payment, Ledger, CreditLedger)
    with facade.admin.db.transaction() as session:
        product = session.get(Product, old["id"])
        product.content = product.content | {"legacy_item_data_preserved_in": deepcopy(metadata)}
        before_content = deepcopy(product.content)
        before_money = (product.price, product.currency, product.included_devices, product.extra_device_price)
        before_counts = {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in money_models
        }
        before_audits = session.scalar(select(func.count()).select_from(Audit))
    text = "Новый текст тарифа"
    premium_ast = telegram_html_to_content(
        '<b>Новый текст тарифа</b> <tg-emoji emoji-id="9007199254740993">🌐</tg-emoji>'
    )
    patch = {field: text}
    expected_ast = telegram_html_to_content(text)
    if content_mode == "resend_old":
        patch["content"] = deepcopy(before_content)
    elif content_mode == "fresh_premium":
        patch["content"] = {field: premium_ast}
        expected_ast = premium_ast
    facade.bot_product_save(actor, old["id"], patch, "Правка импортированного тарифа", confirmed=True)

    with facade.admin.db.transaction() as session:
        product = session.get(Product, old["id"])
        assert getattr(product, field) == text
        assert product.content[field] == expected_ast
        other_field = "description" if field == "name" else "name"
        assert product.content[other_field] == before_content[other_field]
        assert product.content["legacy_item_data_preserved_in"] == metadata
        assert product.version == old["version"] + 1
        assert (product.price, product.currency, product.included_devices, product.extra_device_price) == before_money
        assert {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in money_models
        } == before_counts
        assert session.scalar(select(func.count()).select_from(Audit)) == before_audits + 1
        audit = session.scalar(
            select(Audit).where(Audit.action == "catalog_update", Audit.target == old["id"])
        )
        assert audit is not None
    current = next(item for item in facade.bot_products(actor)["items"] if item["id"] == old["id"])
    assert current["content"][field] == expected_ast
    assert current["content"]["legacy_item_data_preserved_in"] == metadata
    if content_mode == "fresh_premium":
        assert 'emoji-id="9007199254740993"' in render_classic(expected_ast, custom_emoji=True)
