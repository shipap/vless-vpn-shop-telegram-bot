from decimal import Decimal
from types import SimpleNamespace

from sunrise_infrastructure.notifications import NotificationService, validate_telegram_content
from sunrise_infrastructure.sale_notifications import paid_staff_sale_content


def test_paid_notice_title_body_and_buttons_use_configured_premium_icons():
    theme = {
        "icons": {
            key: {"custom_emoji_id": str(index), "fallback": "✅"}
            for index, key in enumerate(
                [
                    "success_heading",
                    "subscriptions",
                    "main",
                    "profile",
                    "products",
                    "payment_sbp",
                    "quote_price",
                    "promo",
                    "quote_bonus",
                ],
                91001,
            )
        }
    }
    payload = {
        "kind": "payment_received",
        "audience": "account",
        "title": "Заказ оплачен",
        "body": "Оплата подтверждена.",
        "nodes": [],
    }
    result = NotificationService._notice_presentation(theme, payload)
    rendered = validate_telegram_content(
        result["title"], result["body"], result["nodes"], title_nodes=result["title_nodes"]
    )
    assert 'emoji-id="91001"' in rendered and "Заказ оплачен" in rendered
    buttons = NotificationService._paid_payment_buttons(result)
    assert buttons[0][0]["icon_custom_emoji_id"] == "91002"
    assert buttons[1][0]["icon_custom_emoji_id"] == "91003"
    assert buttons[1][0]["style"] == "danger"


def test_staff_notice_reports_realized_promo_discount_separately_from_spent_bonus():
    account = SimpleNamespace(telegram_username="owner_user", telegram_id=101, email=None)
    order = SimpleNamespace(source="bot", amount=Decimal("1"), currency="RUB")
    _, body = paid_staff_sale_content(
        account,
        order,
        {
            "action": "purchase",
            "product_name": "Тариф",
            "period_days": 30,
            "payment_method": {"id": "sbp"},
            "promo_code": "BIG",
            "promo_discount_amount": "98",
            "bonus_amount": "0",
        },
    )
    assert "Промокод BIG: 98.00 RUB" in body
    assert "Бонусы:" not in body


def test_purchase_notice_uses_immutable_product_emoji_without_raw_html():
    theme = {"icons": {}}
    payload = {
        "kind": "sale",
        "audience": "staff",
        "title": "Новая покупка!",
        "body": "Товар: Подписка: Старое название (30 дн.)",
        "nodes": [],
        "product_name": "Старое название",
        "product_content_name": [
            {"type": "emoji", "id": "99901", "fallback": "⬜️", "label": "Тариф"},
            {"type": "text", "text": " Старое название"},
        ],
    }
    result = NotificationService._notice_presentation(theme, payload)
    rendered = validate_telegram_content(
        result["title"], result["body"], result["nodes"], title_nodes=result["title_nodes"]
    )
    assert 'emoji-id="99901"' in rendered


def test_generic_multiline_and_long_email_remain_plaintext():
    for body in ["\n".join(["line"] * 51), "x" * 10000]:
        payload = {"kind": "generic", "audience": "account", "title": "Title", "body": body, "nodes": []}
        result = NotificationService._notice_presentation({"icons": {}}, payload)
        assert result["body"] == body and result["nodes"] == []


def test_product_named_like_operation_preserves_operation_prefix():
    payload = {
        "kind": "sale",
        "audience": "staff",
        "title": "Title",
        "body": "Товар: Подписка: Подписка (30 дн.)",
        "nodes": [],
        "product_name": "Подписка",
        "product_content_name": [
            {"type": "emoji", "id": "99901", "fallback": "⬜️", "label": "Тариф"},
            {"type": "text", "text": " Подписка"},
        ],
    }
    result = NotificationService._notice_presentation({"icons": {}}, payload)
    rendered = validate_telegram_content(result["title"], result["body"], result["nodes"])
    assert "Товар: Подписка: <b><tg-emoji" in rendered


def test_product_colon_and_invalid_legacy_content_do_not_strand_notification():
    payload = {
        "kind": "sale",
        "title": "Title",
        "body": "Товар: Подписка: Тариф: Плюс (30 дней)",
        "nodes": [],
        "product_name": "Тариф: Плюс",
        "product_content_name": [
            {"type": "emoji", "id": "99901", "fallback": "⬜️", "label": "Тариф"},
            {"type": "text", "text": " Тариф: Плюс"},
        ],
    }
    result = NotificationService._notice_presentation({"icons": {"products": "invalid"}}, payload)
    assert 'emoji-id="99901"' in validate_telegram_content(result["title"], result["body"], result["nodes"])
    payload["product_content_name"] = [{"type": "unsupported"}]
    result = NotificationService._notice_presentation({"icons": []}, payload)
    assert "Тариф: Плюс" in validate_telegram_content(result["title"], result["body"], result["nodes"])


def test_staff_title_and_owner_controls_reuse_configured_base_icons():
    payload = {"kind": "sale", "title": "Новая покупка!", "body": "Оплачено", "nodes": []}
    theme = {
        "icons": {
            "history_heading": {"custom_emoji_id": "901"},
            "error_heading": {"custom_emoji_id": "902"},
            "revoke": {"custom_emoji_id": "903"},
        }
    }
    result = NotificationService._notice_presentation(theme, payload)
    assert 'emoji-id="901"' in validate_telegram_content(
        result["title"], result["body"], title_nodes=result["title_nodes"]
    )
    assert result["notice_icons"]["admin_warn"]["id"] == "902"
    assert result["notice_icons"]["admin_block"]["id"] == "903"
