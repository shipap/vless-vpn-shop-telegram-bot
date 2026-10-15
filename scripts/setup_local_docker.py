"""Создание отдельного синтетического Docker-стенда; production здесь не включается."""

import argparse
import fnmatch
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def owned_path(path):
    target = Path(path).absolute()
    for part in (target, *target.parents):
        if part.is_symlink() or (
            part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Ссылки и reparse-пути для локальных секретов запрещены.")
    return target


def unignores(pattern, protected):
    variants = {pattern.lstrip("/").rstrip("/")}
    while any(value.startswith("**/") for value in variants):
        expanded = {value[3:] for value in variants if value.startswith("**/")}
        if expanded <= variants:
            break
        variants |= expanded
    for target in protected:
        candidates = {target, *(part.as_posix() for part in Path(target).parents if part.as_posix() != ".")}
        for value in variants:
            tested = candidates | set(Path(target).parts) if "/" not in value else candidates
            if any(fnmatch.fnmatchcase(candidate, value) for candidate in tested):
                return True
    return False


def check_ignored(root, *, env_file=".env.docker", include_private=False):
    protected = [env_file]
    if include_private:
        protected += [".private", *(f".private/production/{name}" for name in (
            "runtime.env", "admission.json", "source-manifest.json", "install-plan.json", "install-input.json"
        ))]
    for name in (".gitignore", ".dockerignore"):
        source = owned_path(root / name)
        patterns = [line.strip() for line in source.read_text(encoding="utf-8").splitlines()]
        if (
            ".env.*" not in patterns
            or include_private and not {".private", ".private/"}.intersection(patterns)
            or any(
            line.startswith("!") and (
                unignores(line[1:], protected)
                or include_private and unignores(line[1:].lstrip("/").split("/", 1)[0], [".private"])
            )
            for line in patterns
            )
        ):
            raise ValueError("Сначала исключите .env.docker из Git и Docker-контекста.")


def free_port(preferred, excluded=()):
    if type(preferred) is not int or not 1024 <= preferred <= 65535 or preferred in excluded:
        raise ValueError("Нужны разные локальные порты от 1024 до 65535.")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
        except OSError:
            raise ValueError("Выбранный локальный порт уже занят.") from None
    return preferred


def choose_port(requested, preferred, excluded=()):
    if requested is not None:
        return free_port(requested, excluded)
    try:
        return free_port(preferred, excluded)
    except ValueError:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            candidate = probe.getsockname()[1]
        return free_port(candidate, excluded)


def prepare(root=ROOT, *, landing_port=None, dashboard_port=None):
    root = owned_path(root)
    check_ignored(root)
    target = owned_path(root / ".env.docker")
    if target.exists():
        if not target.is_file():
            raise ValueError(".env.docker должен быть обычным файлом.")
        return {"created": False}
    landing = choose_port(landing_port, 3100)
    dashboard = choose_port(dashboard_port, 3200, (landing,))
    database_password, redis_password, signing_key = (secrets.token_hex(32) for _ in range(3))
    values = {
        "LOCAL_COMPOSE_PROJECT": "vpn-shop-local-" + secrets.token_hex(6),
        "LOCAL_LANDING_PORT": str(landing),
        "LOCAL_DASHBOARD_PORT": str(dashboard),
        "LOCAL_POSTGRES_PASSWORD": database_password,
        "LOCAL_REDIS_PASSWORD": redis_password,
        "SUNRISE_DATABASE_URL": f"postgresql+psycopg://sunrise_app:{database_password}@127.0.0.1:55439/sunrise_test",
        "SUNRISE_REDIS_URL": f"redis://:{redis_password}@127.0.0.1:56379/0",
        "SUNRISE_SECRET_KEY": signing_key,
        "SUNRISE_RUNTIME_MODE": "isolated",
        "SUNRISE_LOCAL_TEST": "true",
        "INSTANCE_CONFIG_PATH": "",
        "APP_NAME": "VLESS VPN Shop",
        "APP_SLUG": "vpn-shop",
        "VISUAL_PROFILE": "neutral",
        "PUBLIC_SITE_URL": f"http://127.0.0.1:{landing}",
        "DASHBOARD_URL": f"http://127.0.0.1:{dashboard}",
        "SUNRISE_ALLOWED_HOSTS": json.dumps(["127.0.0.1", "localhost"], separators=(",", ":")),
        "SUNRISE_ALLOWED_ORIGINS": json.dumps([f"http://127.0.0.1:{dashboard}"], separators=(",", ":")),
        "SUNRISE_EMAIL_ALLOWLIST": "[]",
        "SUNRISE_TEST_TELEGRAM_RECIPIENTS": "",
        "SUNRISE_TEST_BOT_IDS": "",
        "SUNRISE_BOT_TEST_TOKEN": "",
        "SUNRISE_BOT_TOKEN": "",
        "SUNRISE_TELEGRAM_MINIAPP_ENABLED": "false",
        "SUNRISE_TURNSTILE_TEST_BYPASS": "true",
        "SUNRISE_COOKIE_SECURE": "false",
        "SUNRISE_MAIL_DIR": "/app/.private/mail",
        **{
            f"SUNRISE_{service}_TEST_AUTHORIZED": "false"
            for service in ("ROLLYPAY", "RECURRING", "PANEL", "PANEL_WEBHOOK", "SMTP", "PUSH", "EMOJI")
        },
    }
    body = (
        "# Локальный синтетический Docker-стенд. Не публиковать; production здесь не включается.\n"
        "# Секреты созданы случайно. Повторная настройка сохраняет этот файл.\n"
        "# Только localhost; реальные интеграции и получатели отключены.\n"
        + "".join(f"{key}={value}\n" for key, value in values.items())
    )
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        owned_path(target)
        if not target.is_file():
            raise ValueError(".env.docker должен быть обычным файлом.") from None
        return {"created": False}
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    return {"created": True, "landing_port": landing, "dashboard_port": dashboard}


def initialize():
    """Только явная команда нового локального стенда; импорт не делает DDL."""
    from sunrise_infrastructure.bootstrap_config import initial_legal_documents
    from sunrise_infrastructure.settings import Settings

    config = Settings(_env_file=None)
    if config.runtime_mode != "isolated" or not config.local_test or any(
        getattr(config, name) for name in Settings.model_fields if name.endswith("_test_authorized")
    ):
        raise ValueError("Инициализация допускает только локальный стенд без внешних провайдеров.")
    initial_legal_documents(config)
    subprocess.run(  # noqa: S603 -- fixed local command; existing migration guards remain in force
        [sys.executable, "scripts/run_local.py", "migrate"], cwd=ROOT, check=True
    )
    subprocess.run(  # noqa: S603 -- bootstrap runs only after a successful guarded migration
        [sys.executable, "scripts/run_local.py", "bootstrap"], cwd=ROOT, check=True
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Отдельный локальный Docker-стенд без реальных интеграций")
    parser.add_argument("command", choices=("prepare", "init"), nargs="?", default="prepare")
    parser.add_argument("--landing-port", type=int)
    parser.add_argument("--dashboard-port", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize()
            return 0
        result = prepare(landing_port=args.landing_port, dashboard_port=args.dashboard_port)
        if result["created"]:
            print("Создан .env.docker; значения секретов не выводятся.")
            print(f"Лендинг: http://127.0.0.1:{result['landing_port']}")
            print(f"Кабинет: http://127.0.0.1:{result['dashboard_port']}")
        else:
            print(".env.docker уже существует; настройки сохранены без изменений.")
        print("docker compose --env-file .env.docker -f infra/compose.standalone.json build")
        print("docker compose --env-file .env.docker -f infra/compose.standalone.json up -d")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("Подготовка не выполнена: проверьте пути, ignore-правила, порты и локальный режим.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
