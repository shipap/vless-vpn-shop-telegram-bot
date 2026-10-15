"""Actual Admin methods over synthetic transactions, without SQL or transport."""

from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sunrise_core.errors import DomainError
from sunrise_core.telegram_content import render_plain, telegram_html_to_content
from sunrise_infrastructure.admin import Admin


class SyntheticDatabase:
    def __init__(self, row):
        self.row = row
        self.queries = []

    @contextmanager
    def transaction(self):
        before = deepcopy(self.row)
        database = self

        class Session:
            def execute(self, query):
                database.queries.append(str(query.compile(dialect=postgresql.dialect())))

            def scalar(self, query):
                self.execute(query)
                return database.row

            def get(self, model, key):
                return database.row

            def add(self, row):
                database.row = row

        try:
            yield Session()
        except Exception:
            self.row = before
            raise


def admin_for(row):
    admin = Admin.__new__(Admin)
    admin.db = SyntheticDatabase(row)
    admin.commerce = SimpleNamespace(_setting=lambda session, key, default: default)
    admin._row = lambda product, **kwargs: deepcopy(vars(product))
    admin.audit_records = []
    admin.audit = lambda session, *args: admin.audit_records.append(deepcopy(args))
    return admin


def product():
    return SimpleNamespace(
        id="synthetic-product",
        name="Тариф",
        description="Старое описание",
        content={
            "name": [{"type": "text", "text": "Название AST"}],
            "description": [{"type": "text", "text": "Старое описание"}],
        },
        presentation={},
        version=7,
        price=Decimal("99.00"),
        included_devices=4,
    )


@pytest.mark.parametrize("resend_old_ast", [False, True])
def test_plain_description_edit_replaces_only_stale_description_ast(resend_old_ast):
    row = product()
    admin = admin_for(row)
    raw = '<b>Новое</b> <tg-emoji emoji-id="9007199254740993">🌐</tg-emoji>'
    patch = {"description": raw}
    if resend_old_ast:
        patch["content"] = deepcopy(row.content)
    before_patch = deepcopy(patch)
    old_name = deepcopy(row.content["name"])
    result = admin.catalog_update("synthetic-owner", row.id, patch, "Новое описание")
    assert result["content"]["description"] == telegram_html_to_content(raw)
    assert result["content"]["name"] == old_name
    assert result["price"] == Decimal("99.00") and result["included_devices"] == 4
    assert result["version"] == 8
    assert patch == before_patch
    assert "FOR UPDATE" in admin.db.queries[0]
    assert admin.audit_records[-1][1:3] == ("catalog_update", row.id)


def test_explicit_new_ast_wins_and_preserves_other_content():
    row = product()
    admin = admin_for(row)
    explicit = [{"type": "emoji", "id": "9007199254740993", "fallback": "🌐", "label": "Эмодзи"}]
    result = admin.catalog_update(
        "synthetic-owner",
        row.id,
        {"description": "Plain fallback", "content": {"description": explicit}},
        "Rich описание",
    )
    assert result["content"]["description"] == explicit
    assert result["content"]["name"] == row.content["name"]


def test_unchanged_plain_description_preserves_existing_premium_ast():
    row = product()
    row.content["description"] = telegram_html_to_content(
        '<tg-emoji emoji-id="9007199254740993">🌐</tg-emoji>'
    )
    original = deepcopy(row.content)
    admin = admin_for(row)
    result = admin.catalog_update(
        "synthetic-owner", row.id, {"description": row.description}, "Без изменения текста"
    )
    assert result["content"] == original


@pytest.mark.parametrize("new_ast", [False, True])
def test_null_description_is_omitted_while_explicit_new_ast_remains_supported(new_ast):
    row = product()
    old_description = row.description
    old_content = deepcopy(row.content)
    patch = {"description": None}
    if new_ast:
        patch["content"] = {"description": telegram_html_to_content("<b>Новый AST</b>")}
    admin = admin_for(row)
    result = admin.catalog_update("synthetic-owner", row.id, patch, "Совместимый null patch")
    assert result["description"] == old_description
    assert result["content"]["name"] == old_content["name"]
    assert result["content"]["description"] == (
        patch["content"]["description"] if new_ast else old_content["description"]
    )
    assert result["price"] == Decimal("99.00") and result["included_devices"] == 4


def test_explicit_empty_description_removes_old_ast_and_preserves_name():
    row = product()
    admin = admin_for(row)
    result = admin.catalog_update("synthetic-owner", row.id, {"description": ""}, "Очистить описание")
    assert result["content"]["description"] == []
    assert result["content"]["name"] == row.content["name"]


@pytest.mark.parametrize(
    "patch",
    [
        {"description": 123},
        {"content": []},
        {"description": "Новое", "content": {"description": [{"type": "unsafe"}]}},
    ],
)
def test_invalid_description_or_content_rolls_back_without_audit(patch):
    row = product()
    before = deepcopy(vars(row))
    admin = admin_for(row)
    with pytest.raises(DomainError):
        admin.catalog_update("synthetic-owner", row.id, patch, "Некорректный контент")
    assert vars(admin.db.row) == before
    assert admin.audit_records == []


def public_value():
    return {
        "faq": [],
        "how_to_connect": [],
        "promo_channels": [{"title": "Канал", "url": "https://news.example.test"}],
        "catalog_description": "Сохранённое intro",
        "catalog_description_content": [{"type": "text", "text": "Сохранённое intro"}],
    }


def test_public_content_update_merges_locked_current_fields_and_audits_once():
    old = public_value()
    admin = admin_for(SimpleNamespace(key="public_content", value=deepcopy(old)))
    patch = {"faq": [{"id": "fresh", "question": "Вопрос", "answer": []}], "how_to_connect": []}
    result = admin.update_public_content("synthetic-owner", patch, "Изменение FAQ")
    assert result == old | patch
    assert admin.db.row.value == result
    assert old == public_value()
    assert patch.keys() == {"faq", "how_to_connect"}
    assert "pg_advisory_xact_lock" in admin.db.queries[0]
    assert "FOR UPDATE" in admin.db.queries[1]
    assert len(admin.audit_records) == 1
    assert admin.audit_records[0] == (
        "synthetic-owner",
        "public_content_update",
        "public_content",
        "Изменение FAQ",
        old,
        result,
    )


def test_partial_intro_update_validates_merged_current_value():
    old = public_value()
    admin = admin_for(SimpleNamespace(key="public_content", value=deepcopy(old)))
    nodes = telegram_html_to_content("<b>Новое intro</b>")
    result = admin.update_public_content(
        "synthetic-owner",
        {"catalog_description": render_plain(nodes), "catalog_description_content": nodes},
        "Изменить intro",
    )
    assert result["catalog_description_content"] == nodes
    assert result["faq"] == old["faq"] and result["promo_channels"] == old["promo_channels"]


@pytest.mark.parametrize("reason", ["", "x", "x" * 501])
def test_public_content_keeps_existing_reason_guard_before_transaction(reason):
    admin = admin_for(SimpleNamespace(key="public_content", value=public_value()))
    with pytest.raises(DomainError, match="Укажите причину"):
        admin.update_public_content("synthetic-owner", {"faq": [], "how_to_connect": []}, reason)
    assert admin.db.queries == [] and admin.audit_records == []


def test_new_public_content_row_is_serialized_and_bad_merged_data_never_persists():
    admin = admin_for(None)
    result = admin.update_public_content(
        "synthetic-owner", {"faq": [], "how_to_connect": []}, "Создать контент"
    )
    assert admin.db.row.value == result
    assert "pg_advisory_xact_lock" in admin.db.queries[0]
    assert "FOR UPDATE" in admin.db.queries[1]
    before = deepcopy(admin.db.row.value)
    with pytest.raises(DomainError):
        admin.update_public_content("synthetic-owner", {"unknown": "field"}, "Ошибка поля")
    assert admin.db.row.value == before
