from urllib.parse import parse_qs, urlsplit

import pytest
from sunrise_core.support_links import support_draft_url
from sunrise_infrastructure.catalog import validate_public_content


def test_unknown_status_cannot_export_arbitrary_private_text():
    url = support_draft_url(
        "https://t.me/synthetic_support",
        kind="subscription",
        identifier="synthetic-sub",
        status="SECRET\nhttps://vpn.secret",
    )
    assert parse_qs(urlsplit(url).query)["text"] == [
        "Нужна помощь по подписке.\nID: synthetic-sub\nСтатус: Не определён"
    ]


@pytest.mark.parametrize(
    "status,label",
    [
        ("active", "Действует"),
        ("paid", "Оплачено"),
        ("paid_provisioning_pending", "Оплачено, подключаем"),
        ("reconciliation_required", "Требуется сверка оплаты"),
        ("refund_review", "Возврат на проверке"),
        ("unknown", "Не определён"),
    ],
)
def test_support_draft_uses_safe_russian_status_labels(status, label):
    url = support_draft_url("https://t.me/synthetic_support", kind="order", identifier="o1", status=status)
    assert parse_qs(urlsplit(url).query)["text"] == [f"Нужна помощь по заказу.\nID: o1\nСтатус: {label}"]


@pytest.mark.parametrize(
    "value",
    [
        "https://support.example.test/form",
        "https://t.me/share/url?url=https://example.test",
        "https://t.me/+invite",
        "https://t.me/m/business",
        "https://t.me/synthetic_bot",
        "https://t.me/synthetic_support?start=link",
        "https://t.me/addstickers",
        "https://t.me/addemoji",
        "https://t.me/boost",
        "https://t.me/invoice",
    ],
)
def test_nonpersonal_or_deep_links_keep_configured_destination_without_draft(value):
    assert support_draft_url(value, kind="order", identifier="o1", status="paid") == value


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "https://user:secret@t.me/synthetic_support",
        "//t.me/synthetic_support",
        "https://t.me/synthetic_support\nInjected",
    ],
)
def test_unsafe_support_url_is_not_interactive(value):
    assert support_draft_url(value, kind="order", identifier="o1", status="paid") is None


def test_invalid_identifier_falls_back_without_exporting_connection_material():
    base = "https://t.me/synthetic_support"
    assert support_draft_url(base, kind="subscription", identifier="vpn://secret", status="active") == base


def test_practical_starters_validate_as_current_admin_database_content():
    import json
    from pathlib import Path

    rows = json.loads(
        (Path(__file__).resolve().parents[2] / "packages/web-ui/practical-faq.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(rows) == 6
    assert validate_public_content({"faq": rows, "how_to_connect": []})["faq"] == rows


def test_payment_faq_explains_automatic_delivery_without_manual_refresh():
    import json
    from pathlib import Path

    rows = json.loads(
        (Path(__file__).resolve().parents[2] / "packages/web-ui/practical-faq.example.json").read_text(
            encoding="utf-8"
        )
    )
    answer = next(row["answer"] for row in rows if row["id"] == "help_payment_access")
    text = " ".join(child.get("text", "") for node in answer for child in node.get("children", []))
    assert "Проверить статус" not in text
    assert "автоматически" in text
    assert "не оплачивайте повторно" in text
