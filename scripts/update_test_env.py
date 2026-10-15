"""Refresh the private environment from its actual schema without exposing credentials."""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from dotenv import dotenv_values

COMMENTS = {
    "admin_telegram_allowlist": "JSON проверенных Telegram ID строками; доступ учитывает owner_id, capabilities и MFA.",
    "database_url": "DSN отдельной PostgreSQL: только localhost:55439/sunrise_test. Секрет; создаётся локально.",
    "redis_url": "DSN отдельного Redis на localhost:56379. Секрет; создаётся локально.",
    "secret_key": "Секрет подписи сессий, минимум 32 символа; сгенерируйте для своего экземпляра.",
    "local_test": "true оставляет изолированный режим и почтовый sink; не переключать для подключения production.",
    "admin_email_allowlist": "Совместимость со старыми настройками. Email сам по себе не выдаёт административный доступ.",
    "admin_totp_secrets": "Секрет: JSON соответствий email → TOTP-ключ; не публиковать.",
    "email_allowlist": "JSON адресов для тестовых писем. В локальном sink разрешены только адреса .test.",
    "owner_email": "Email владельца, предоставленный для будущего тестового SMTP; сам по себе не выдаёт админские права.",
    "landing_test_domain": "Согласованный тестовый домен лендинга. Запись не публикует сайт и не меняет DNS.",
    "dashboard_test_domain": "Согласованный тестовый домен личного кабинета. Запись не публикует сайт и не меняет DNS.",
    "legal_offer_url": "Каноническая ссылка на оферту. Только начальное заполнение БД; далее менять через админку.",
    "legal_privacy_url": "Каноническая ссылка на политику. Только начальное заполнение БД; далее менять через админку.",
    "legal_offer_version": "Версия оферты, подтверждённая владельцем: v1. Новая версия требует нового согласия.",
    "legal_privacy_version": "Версия политики, подтверждённая владельцем: v1. Это ID, а не дата публикации.",
    "rollypay_api_key": "Секрет: API key только ТЕСТОВОЙ кассы RollyPay; получить в её кабинете.",
    "rollypay_signing_secret": "Секрет: ключ проверки подписей callback ТЕСТОВОЙ кассы; не API key.",
    "rollypay_terminal": "ID terminal/cashbox отдельной ТЕСТОВОЙ кассы RollyPay.",
    "rollypay_base_url": "Официальный HTTPS API base URL тестового RollyPay, подтверждённый документацией провайдера.",
    "webhook_base_url": "HTTPS origin без пути; для генерируемого production runtime используйте origin кабинета. Полные callback URL /api/v1/webhooks/rollypay и /api/v1/webhooks/remnawave задаются у провайдеров отдельно; см. docs/DEPLOYMENT.md.",
    "payment_allowed_hosts": "JSON разрешённых hostname тестового API RollyPay; пустой список блокирует запросы.",
    "recurring_test_authorized": "Оставить false до отдельного разрешения владельца на реальные тестовые списания.",
    "panel_test_authorized": "Оставить false до проверки отдельного TEST Remnawave и разрешённых ресурсов.",
    "panel_contract": "Выберите поддерживаемый контракт своей панели; базовая совместимость проекта — Remnawave 3.4.5.",
    "panel_base_url": "HTTPS base URL Remnawave; только разрешённые отдельные тестовые ресурсы, существующих пользователей не менять.",
    "panel_token": "Секрет: API token отдельной TEST-панели Remnawave.",
    "panel_test_squads": "JSON UUID разрешённых тестовых squads; не копировать production UUID.",
    "panel_test_uuids": "JSON UUID только тестовых пользователей Remnawave.",
    "smtp_test_authorized": "Оставить false до проверки TEST SMTP и разрешённого получателя.",
    "smtp_host": "Hostname отдельного тестового SMTP; получить у почтового провайдера.",
    "smtp_port": "Порт TEST SMTP: обычно 587 для STARTTLS или 465 для SMTPS; сверить у провайдера.",
    "smtp_mode": "starttls или smtps; незашифрованная внешняя почта запрещена.",
    "smtp_sender": "Адрес отправителя, разрешённый TEST SMTP; это не email получателя.",
    "smtp_username": "Логин только тестового SMTP.",
    "smtp_password": "Секрет: пароль/пароль приложения только тестового SMTP.",
    "turnstile_site_key": "Публичный site key отдельного TEST Turnstile для согласованного hostname.",
    "turnstile_secret": "Секрет: server-side secret отдельного TEST Turnstile; не вставлять в frontend.",
    "turnstile_hostname": "Точный hostname, для которого выпущены TEST Turnstile ключи.",
}


def quote(value: str) -> str:
    if any(c in value for c in "\r\n\0"):
        raise ValueError("Значение переменной должно занимать одну строку")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def merge_template(template: Path, target: Path, overrides: dict[str, str]):
    observed = target.read_bytes() if target.exists() else None
    values = dict(dotenv_values(template, interpolate=False))
    if target.exists():
        values.update(dotenv_values(target, interpolate=False))
    values.update(overrides)
    encoded = {key: quote(value or "") for key, value in values.items()}
    lines = []
    emitted = set()
    for line in template.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match:
            key = match.group(1)
            lines.append(f"{key}={encoded[key]}")
            emitted.add(key)
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in encoded.items() if key not in emitted)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if (target.read_bytes() if target.exists() else None) != observed:
        temporary.unlink()
        raise ValueError("Файл .env изменён другим процессом; правки владельца сохранены")
    temporary.replace(target)


def compact_environment(target: Path, defaults: dict[str, str], editable: set[str], comments: dict[str, str]):
    observed = target.read_bytes()
    values = dict(dotenv_values(target, interpolate=False))

    def equivalent(value, default):
        if value == default:
            return True
        try:
            return json.loads(value) == json.loads(default)
        except (ValueError, TypeError):
            return False

    kept = {
        key: value or ""
        for key, value in values.items()
        if key in editable or key not in defaults or not equivalent(value or "", defaults[key])
    }
    lines = [
        "# Заполняйте только верхние разделы. Секреты не публиковать.",
        "# После изменений нужен перезапуск. Пустые ключи отключают интеграцию.",
    ]
    for prefix, title in [
        ("SUNRISE_ROLLYPAY_", "RollyPay — только TEST-касса"),
        ("SUNRISE_PANEL_", "Remnawave — только разрешённые тестовые ресурсы"),
        ("SUNRISE_SMTP_", "Почта — TEST SMTP"),
        ("SUNRISE_TURNSTILE_", "Turnstile — отдельные тестовые ключи"),
    ]:
        keys = [key for key in kept if key in editable and key.startswith(prefix)]
        if not keys:
            continue
        lines.extend(["", "# " + title])
        for key in keys:
            lines.extend(["# " + comments.get(key, key), f"{key}={quote(kept.pop(key))}"])
    lines.extend(["", "# Служебные настройки стенда и тестовый бот — уже заполнены, менять не нужно."])
    lines.extend(f"{key}={quote(value)}" for key, value in kept.items())
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if target.read_bytes() != observed:
        temporary.unlink()
        raise ValueError("Файл .env изменён во время сокращения; правки владельца сохранены")
    temporary.replace(target)


def verify_private(root: Path):
    git = shutil.which("git")
    if not git:
        raise ValueError("Git необходим для проверки приватности .env")
    ignored = subprocess.run(  # noqa: S603 -- resolved Git executable, fixed arguments, no shell
        [git, "check-ignore", "--quiet", ".env"], cwd=root, capture_output=True
    )
    tracked = subprocess.run(  # noqa: S603 -- resolved Git executable, fixed arguments, no shell
        [git, "ls-files", "--error-unmatch", ".env"], cwd=root, capture_output=True
    )
    docker_rules = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    if ignored.returncode != 0 or tracked.returncode == 0 or ".env" not in docker_rules:
        raise ValueError("Перед записью .env требуются исключения Git и Docker")


def build_template(root: Path):
    from pydantic import AliasChoices
    from pydantic_core import PydanticUndefined
    from sunrise_infrastructure.instance_config import InstanceConfig
    from sunrise_infrastructure.settings import Settings

    lines = [
        "# Только отдельный тестовый стенд. Секреты не публиковать.",
        "# Изменения требуют перезапуска API/worker/bot. Пустые интеграции отключены.",
        "# Списки и словари — JSON; ID Telegram/Remnawave хранятся строками.",
    ]
    for name, field in Settings.model_fields.items():
        key = (
            field.validation_alias.choices[0]
            if isinstance(field.validation_alias, AliasChoices)
            else "SUNRISE_" + name.upper()
        )
        default = field.get_default(call_default_factory=True)
        if name in {
            "app_name",
            "app_short_name",
            "app_slug",
            "app_description",
            "visual_profile",
            "accent_color",
        }:
            default = InstanceConfig.model_fields[name].get_default()
        if default is PydanticUndefined or default is None:
            default = ""
        elif isinstance(default, (list, dict, bool)):
            default = json.dumps(default, ensure_ascii=False)
        else:
            default = str(default)
        if name in {"database_url", "redis_url", "secret_key"}:
            default = ""
        sensitive = not field.repr
        lines.append(
            "# "
            + COMMENTS.get(
                name,
                ("Секрет: только локальный .env. " if sensitive else "")
                + "Настройка "
                + name
                + "; по умолчанию интеграция отключена, требуется перезапуск.",
            )
        )
        lines.append(f"{key}={quote(default)}")
    extra = {
        "SUNRISE_TELEGRAM_CUSTOM_EMOJI_VERIFIED": "false",
        "SUNRISE_BOT_TEST_TOKEN": "",
        "SUNRISE_TEST_BOT_IDS": "",
        "SUNRISE_TEST_TELEGRAM_RECIPIENTS": "",
        "SUNRISE_TEST_DATABASE_URL": "",
    }
    for key, value in extra.items():
        comment = {
            "SUNRISE_TELEGRAM_CUSTOM_EMOJI_VERIFIED": "Заполняется автоматически после проверки отправки Premium-эмодзи у разрешённого TEST-бота.",
            "SUNRISE_BOT_TEST_TOKEN": "Секрет: токен отдельного тестового бота из BotFather. Существующий токен сохранён.",
            "SUNRISE_TEST_BOT_IDS": "Числовые ID разрешённых тестовых ботов через запятую; проверяются по getMe.",
            "SUNRISE_TEST_TELEGRAM_RECIPIENTS": "Числовые Telegram ID разрешённых получателей тестового бота через запятую.",
            "SUNRISE_TEST_DATABASE_URL": "DSN того же общего синтетического backend; run_local bot задаёт автоматически.",
        }[key]
        lines.extend(["# " + comment, f"{key}={quote(value)}"])
    (root / ".env.example").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dict(dotenv_values(root / ".env.example", interpolate=False))


def main():
    root = Path(__file__).resolve().parents[1]
    verify_private(root)
    defaults = build_template(root)
    if (root / ".env").exists():
        backup = root / ".private/env-backups"
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / ".env", backup / f"before-compact-{time.time_ns()}.env")
    merge_template(root / ".env.example", root / ".env", {})
    editable_names = {
        "rollypay_api_key",
        "rollypay_signing_secret",
        "rollypay_terminal",
        "rollypay_base_url",
        "panel_base_url",
        "panel_token",
        "smtp_host",
        "smtp_port",
        "smtp_mode",
        "smtp_sender",
        "smtp_username",
        "smtp_password",
        "turnstile_site_key",
        "turnstile_secret",
        "turnstile_hostname",
    }
    editable = {"SUNRISE_" + name.upper() for name in editable_names}
    compact_environment(
        root / ".env",
        defaults,
        editable,
        {"SUNRISE_" + key.upper(): value for key, value in COMMENTS.items()},
    )
    print("Шаблон обновлён; локальные секреты сохранены. Внешние интеграции не включены.")


if __name__ == "__main__":
    main()
