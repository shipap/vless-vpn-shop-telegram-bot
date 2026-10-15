from sunrise_infrastructure.mail_templates import render_mail


def test_security_notice_renders_safe_unlink_button_and_plain_link():
    result = render_mail(
        "Аккаунты связаны",
        "Если это сделали не вы, подтвердите отвязку в аккаунте.",
        "no-reply@example.test",
        actions=[{"label": "Отвязать Telegram", "url": "https://account.example.test/#account"}],
    )
    assert 'href="https://account.example.test/#account"' in result.html
    assert "Отвязать Telegram" in result.html
    assert "Отвязать Telegram: https://account.example.test/#account" in result.plain


def test_mail_action_rejects_executable_and_credential_urls():
    result = render_mail(
        "Аккаунты связаны",
        "Уведомление",
        "no-reply@example.test",
        actions=[
            {"label": "Unsafe", "url": "javascript:alert(1)"},
            {"label": "Credentials", "url": "https://user:pass@example.test/"},
        ],
    )
    assert "javascript:" not in result.html + result.plain
    assert "user:pass" not in result.html + result.plain
