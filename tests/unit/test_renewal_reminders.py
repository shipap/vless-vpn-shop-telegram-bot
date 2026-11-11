"""Ступени скидки на продление и текст напоминания без сети и базы."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.renewal_reminders import (
    bucket_is_current,
    near_expiry_percent,
    near_expiry_tiers,
    reminder_bucket,
    reminder_content,
    reminder_window,
)

TIERS = [{"hours": 48, "percent": "15"}, {"hours": 24, "percent": "10"}, {"hours": 12, "percent": "5"}]
POLICY = {"near_expiry_renewal": {"enabled": True, "percent": "10"}, "near_expiry_tiers": TIERS}
LEGACY = {"near_expiry_renewal": {"enabled": True, "percent": "10"}}


@pytest.mark.parametrize(
    "hours,bucket,percent",
    [(49, None, None), (48, "48h", "15"), (25, "48h", "15"), (24, "24h", "10"), (12.5, "24h", "10"),
     (12, "12h", "5"), (0.1, "12h", "5"), (0, "expired", None), (-23, "expired", None), (-25, None, None)],
)  # fmt: skip
def test_tier_buckets_and_percent(hours, bucket, percent):
    seconds = hours * 3600
    assert reminder_bucket(POLICY, seconds) == bucket
    result = near_expiry_percent(POLICY, timedelta(seconds=seconds))
    assert result == (Decimal(percent) if percent else None)
    assert reminder_window(POLICY) == timedelta(hours=48)


def test_legacy_policy_keeps_previous_rules():
    assert reminder_window(LEGACY) == timedelta(days=3)
    assert reminder_bucket(LEGACY, 2 * 86400) == "3d" and reminder_bucket(LEGACY, 3600) == "1d"
    assert near_expiry_percent(LEGACY, timedelta(hours=23)) == Decimal("10")
    assert near_expiry_percent(LEGACY, timedelta(hours=-5)) == Decimal("10")
    assert near_expiry_percent(LEGACY, timedelta(hours=25)) is None
    disabled = {**POLICY, "near_expiry_renewal": {"enabled": False, "percent": "10"}}
    assert near_expiry_percent(disabled, timedelta(hours=30)) is None
    assert reminder_window(disabled) == timedelta(days=3)


@pytest.mark.parametrize(
    "tiers",
    [[{"hours": 48, "percent": "15"}, {"hours": 48, "percent": "5"}], [{"hours": 0, "percent": "5"}],
     [{"hours": 12, "percent": "101"}], [{"hours": 12, "percent": "x"}], [{"hours": 12}], [{"hours": True, "percent": "5"}]],
)  # fmt: skip
def test_invalid_tiers_fail_closed_without_any_discount(tiers):
    policy = {**LEGACY, "near_expiry_tiers": tiers}
    assert near_expiry_tiers(policy) is None
    for hours in (30, 5, -5):
        assert near_expiry_percent(policy, timedelta(hours=hours)) is None
    assert reminder_bucket(policy, 3600) == "1d"  # reminders keep the legacy schedule


def test_text_names_only_the_discount_the_quote_applied():
    moment = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    quote = {"period_days": 30, "total": "212.50", "currency": "RUB"}
    common = {"product_name": "X", "expiry": moment + timedelta(hours=40), "moment": moment, "quote": quote}
    _, applied = reminder_content(POLICY, applied_percent=Decimal("15"), **common)
    assert "Сейчас скидка на продление — 15%." in applied and "со скидкой." in applied
    for other in (None, Decimal("20")):
        _, body = reminder_content(POLICY, applied_percent=other, **common)
        assert "скидка" not in body.lower() and "Продление на 30 дней: 212.50 ₽." in body


def test_queued_reminders_stay_current_only_in_their_bucket():
    assert bucket_is_current(POLICY, "24h", 20 * 3600)
    assert not bucket_is_current(POLICY, "48h", 20 * 3600)
    assert bucket_is_current(POLICY, "3d", 2 * 86400)  # queued before the policy change
    assert not bucket_is_current(POLICY, "1d", 2 * 86400)


def test_reminder_text_names_date_discount_and_next_step():
    moment = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    expiry = moment + timedelta(hours=40)
    quote = {"period_days": 30, "total": "212.50", "currency": "RUB"}
    title, body = reminder_content(POLICY, product_name="Универсальный", expiry=expiry, moment=moment, quote=quote)
    assert title == "Срок подписки заканчивается"
    assert body == (
        "Тариф: Универсальный\n\n"
        "Продлите подписку до 13.11.2026 в 04:00 (МСК), чтобы не потерять доступ к VPN.\n\n"
        "Сейчас скидка на продление — 15%. Через 16 часов она уменьшится до 10%.\n"
        "Продление на 30 дней: 212.50 ₽ со скидкой."
    )
    _, entry = reminder_content(POLICY, product_name="X", expiry=moment + timedelta(hours=48), moment=moment)
    assert "Через 24 часа она уменьшится до 10%." in entry
    _, day = reminder_content(POLICY, product_name="X", expiry=moment + timedelta(hours=24), moment=moment)
    assert "Сейчас скидка на продление — 10%. Через 12 часов она уменьшится до 5%." in day
    _, last = reminder_content(POLICY, product_name="X", expiry=moment + timedelta(hours=3), moment=moment)
    assert "— 5%. Она действует до окончания подписки." in last and "Продление на" not in last
    title, expired = reminder_content(POLICY, product_name="X", expiry=moment - timedelta(hours=1), moment=moment)
    assert title == "Подписка завершилась" and "скидка" not in expired.lower()
    assert "Срок подписки закончился 11.11.2026 в 11:00 (МСК)." in expired
    _, legacy = reminder_content(LEGACY, product_name="X", expiry=moment + timedelta(hours=5), moment=moment)
    assert "Сейчас скидка на продление — 10%." in legacy and "Через" not in legacy


@pytest.mark.parametrize(
    "count,text", [(1, "1 час"), (2, "2 часа"), (5, "5 часов"), (11, "11 часов"), (21, "21 час"), (24, "24 часа")]
)
def test_hours_are_declined(count, text):
    from sunrise_infrastructure.renewal_reminders import _hours

    assert _hours(count) == text


def presentation(body, icons=None):
    payload = {
        "kind": "subscription_expiry",
        "title": "Срок подписки заканчивается",
        "body": body,
        "nodes": [],
        "audience": "account",
        "actions": [
            {"label": "Продлить", "target": "/#subscriptions", "operation": "renew",
             "metadata": {"subscription_id": "sub_1"}}
        ],
    }  # fmt: skip
    return NotificationService._notice_presentation({"icons": icons or {}}, payload)


def test_telegram_presentation_uses_theme_premium_emoji_and_bot_renew_button():
    moment = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    _, body = reminder_content(POLICY, product_name="Тариф А", expiry=moment + timedelta(hours=30), moment=moment)
    icons = {key: {"fallback": "•", "custom_emoji_id": str(index + 5000000000000000000)}
             for index, key in enumerate(["quote_expiry", "subscription", "renew", "quote_discount", "promo"])}  # fmt: skip
    payload = presentation(body, icons)
    assert payload["telegram_only_nodes"] is True
    title_emoji = payload["title_nodes"][0]["children"][0]
    assert title_emoji["label"] == "quote_expiry" and title_emoji["id"] == icons["quote_expiry"]["custom_emoji_id"]
    used = {node.get("label") for node in payload["nodes"] if node["type"] == "emoji"}
    assert {"subscription", "quote_expiry", "quote_discount"} <= used
    assert sum(node["type"] == "br" for node in payload["nodes"]) == 4  # two blank lines between blocks
    assert not any(node.get("text") == "" for node in payload["nodes"])
    buttons = NotificationService._renewal_buttons(payload)
    assert buttons == [[{"text": "Продлить подписку", "callback_data": "notice:renew:sub_1",
                         "icon_custom_emoji_id": icons["renew"]["custom_emoji_id"]}]]  # fmt: skip


def test_renew_button_rejects_unsafe_subscription_ids_and_other_kinds():
    payload = presentation("Тариф: X")
    payload["actions"][0]["metadata"]["subscription_id"] = "bad id:with:colons"
    assert NotificationService._renewal_buttons(payload) == []
    other = presentation("Тариф: X") | {"kind": "sale"}
    assert NotificationService._renewal_buttons(other) == []
    fallback = NotificationService._renewal_buttons(presentation("Тариф: X"))
    assert fallback[0][0]["text"] == "🔄 Продлить подписку" and "icon_custom_emoji_id" not in fallback[0][0]
