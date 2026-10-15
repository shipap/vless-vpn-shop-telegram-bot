import builtins

from sunrise_infrastructure.notifications import validate_telegram_content


def test_backend_rendering_does_not_import_bot_application(monkeypatch):
    original = builtins.__import__

    def without_bot(name, *args, **kwargs):
        if name == "sunrise_bot" or name.startswith("sunrise_bot."):
            raise ImportError("Shared backend must not depend on bot application")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_bot)
    assert validate_telegram_content("Сообщение", "Текст", [{"type": "text", "text": "Текст"}]) == (
        "Сообщение\n\nТекст"
    )
