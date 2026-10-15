"""Отдельный локальный экземпляр того же приложения без внешних интеграций."""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import get_origin
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / ".private" / "demo-instance.json"
FORBIDDEN_PORTS = {8100, 3100, 3200, 8150, 3110, 3210, 3111, 3211}


def clean_environment(inherited, values):
    from pydantic import AliasChoices
    from sunrise_infrastructure.settings import Settings

    aliases = set()
    for field in Settings.model_fields.values():
        if isinstance(field.validation_alias, AliasChoices):
            aliases.update(field.validation_alias.choices)
    cleaned = {
        key: value
        for key, value in inherited.items()
        if not key.startswith(("SUNRISE_", "WEB_")) and key not in aliases
    }
    cleaned.update({key: value for key, value in values.items() if value is not None})
    return cleaned


def schema_database_url(value, schema):
    parsed = make_url(value)
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host not in {"127.0.0.1", "localhost"}
        or parsed.port != 55439
        or parsed.database not in {"sunrise_test", "sunrise_test_backend"}
        or parsed.query
        or not re.fullmatch(r"demo_instance_[a-f0-9]{32}", schema)
    ):
        raise ValueError("Разрешена только новая схема отдельной локальной тестовой БД.")
    return parsed.set(
        database="sunrise_test_backend", query={"options": "-csearch_path=" + schema}
    ).render_as_string(hide_password=False)


def safe_path(path, *, project_root=None):
    from sunrise_infrastructure.instance_assets import reparse

    root = Path(project_root or ROOT).absolute()
    candidate = Path(path).absolute()
    if not candidate.is_relative_to(root) or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Путь демо должен находиться внутри собственного каталога.")
    for component in [candidate, *candidate.parents]:
        if reparse(component):
            raise ValueError("Ссылки и junction в пути демо запрещены.")
        if component == root:
            break
    return candidate


def demo_values(path: Path, *, project_root=None):
    root = Path(project_root or ROOT).absolute()
    path = safe_path(path, project_root=root)
    if path != root / ".env.demo" or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("Требуется собственный ограниченный файл .env.demo.")
    values = dotenv_values(path, interpolate=False)
    return validate_demo_values(values)


def _demo_emails(values):
    from sunrise_infrastructure.mail import synthetic_mail_address

    def addresses(key):
        raw = values.get(key) or "[]"
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError("Нужен список синтетических адресов .test.") from None
        if (
            not isinstance(items, list)
            or len(items) > 100
            or any(not synthetic_mail_address(item) for item in items)
        ):
            raise ValueError("Нужен ограниченный список синтетических адресов .test.")
        return list(dict.fromkeys(item.lower() for item in items))

    emails, admins = addresses("SUNRISE_EMAIL_ALLOWLIST"), addresses("SUNRISE_ADMIN_EMAIL_ALLOWLIST")
    owner = values.get("SUNRISE_OWNER_EMAIL") or None
    if (
        not set(admins).issubset(emails)
        or owner is not None
        and (not synthetic_mail_address(owner) or owner.lower() not in emails)
    ):
        raise ValueError("Администратор и владелец должны входить в разрешённый список .test.")
    return emails, admins, owner.lower() if owner else None


def validate_demo_values(values):
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or value is not None and not isinstance(value, str)
        for key, value in values.items()
    ):
        raise ValueError("Конфигурация демо должна содержать строковые значения.")
    if any(
        value and value.lower() not in {"false", "0", "no"}
        for key, value in values.items()
        if key.endswith("_TEST_AUTHORIZED")
    ):
        raise ValueError("Внешние интеграции в нейтральном экземпляре запрещены.")
    if values.get("SUNRISE_LOCAL_TEST", "").lower() != "true":
        raise ValueError("Требуется изолированный локальный режим.")
    if values.get("INSTANCE_CONFIG_PATH") or values.get("SUNRISE_INSTANCE_CONFIG_PATH"):
        raise ValueError("Демо не читает приватный файл конфигурации.")
    if (values.get("VISUAL_PROFILE") or values.get("SUNRISE_VISUAL_PROFILE") or "neutral") != "neutral":
        raise ValueError("Демо использует нейтральное оформление.")
    for key, value in values.items():
        if (
            value
            and key != "SUNRISE_SECRET_KEY"
            and (
                key.endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))
                or key
                in {
                    "SUNRISE_TURNSTILE_SECRET",
                    "SUNRISE_PUSH_PRIVATE_KEY",
                    "SUNRISE_TELEGRAM_LOGIN_CLIENT_SECRET",
                }
            )
        ):
            raise ValueError("Демо не принимает внешние учётные данные.")
        normalized = key.removeprefix("SUNRISE_")
        if normalized in {
            "OWNER_ID",
            "ADMIN_TELEGRAM_ALLOWLIST",
            "ADMIN_TOTP_SECRETS",
            "TELEGRAM_LOGIN_CLIENT_ID",
            "TELEGRAM_LOGIN_REDIRECT_URI",
            "TEST_BOT_IDS",
            "TEST_TELEGRAM_RECIPIENTS",
            "TELEGRAM_BOT_USERNAME",
            "TELEGRAM_MENU_EMOJI_KEY",
            "BOT_URL",
            "LOGO_ASSET_PATH",
            "FAVICON_ASSET_PATH",
            "HERO_ASSET_PATH",
            "OG_IMAGE_ASSET_PATH",
        } and value not in {None, "", "[]", "{}"}:
            raise ValueError("Демо не принимает приватные цели, права и изображения.")
        if normalized == "TELEGRAM_MINIAPP_ENABLED" and value not in {None, "", "false", "0", "no"}:
            raise ValueError("Telegram-вход в демо отключён.")
    _demo_emails(values)
    return values


def settings_for_demo(values, schema, landing_port, dashboard_port):
    from pydantic import AliasChoices
    from sunrise_infrastructure.settings import Settings

    validate_demo_values(values)
    emails, admins, owner = _demo_emails(values)

    class DemoSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
        ):
            return (init_settings,)

    arguments = {}
    for name, field in Settings.model_fields.items():
        aliases = (
            field.validation_alias.choices
            if isinstance(field.validation_alias, AliasChoices)
            else ["SUNRISE_" + name.upper()]
        )
        for key in aliases:
            if key in values and values[key] not in {None, ""}:
                raw = values[key]
                arguments[name] = json.loads(raw) if get_origin(field.annotation) in {list, dict} else raw
                break
    arguments.update(
        database_url=schema_database_url(values["SUNRISE_DATABASE_URL"], schema),
        instance_config_path=None,
        local_test=True,
        visual_profile="neutral",
        dashboard_url=f"http://localhost:{dashboard_port}",
        landing_url=f"http://localhost:{landing_port}",
        allowed_hosts=["127.0.0.1", "localhost"],
        allowed_origins=[f"http://localhost:{dashboard_port}"],
        mail_dir=str(safe_path(ROOT / ".private/demo-mail")),
        turnstile_test_bypass=True,
        admin_email_allowlist=admins,
        admin_telegram_allowlist=[],
        email_allowlist=emails,
        bot_test_token=None,
        telegram_miniapp_enabled=False,
        telegram_login_client_id=None,
        telegram_login_client_secret=None,
        telegram_login_redirect_uri=None,
        owner_id=None,
        owner_email=owner,
        admin_totp_secrets={},
        smtp_tunnel_port=None,
        panel_tunnel_port=None,
        smtp_test_authorized=False,
        panel_test_authorized=False,
        rollypay_test_authorized=False,
        panel_webhook_test_authorized=False,
        push_test_authorized=False,
        emoji_test_authorized=False,
        recurring_test_authorized=False,
    )
    redis = make_url(arguments.get("redis_url", "redis://127.0.0.1:56379/0"))
    if redis.host not in {"127.0.0.1", "localhost"} or redis.port != 56379:
        raise ValueError("Требуется отдельный локальный Redis.")
    arguments["redis_url"] = redis.set(database="13").render_as_string(hide_password=False)
    return DemoSettings(_env_file=None, **arguments)


def _scoped_database(value):
    parsed = make_url(value)
    options = parsed.query.get("options", "")
    schema = options.removeprefix("-csearch_path=")
    base = parsed.set(query={}).render_as_string(hide_password=False)
    expected = schema_database_url(base, schema)
    if dict(parsed.query) != {"options": "-csearch_path=" + schema} or value != expected:
        raise ValueError("Недопустимая область схемы демо.")
    return schema, base


def prepare_database(settings, seed):
    """Явные миграции только новой схемы; повторный запуск не удаляет данные."""
    from sqlalchemy import text
    from sqlalchemy.schema import CreateSchema, DropSchema
    from sunrise_infrastructure.bootstrap_config import load_bootstrap_seed
    from sunrise_infrastructure.commerce import bootstrap
    from sunrise_infrastructure.db import Database, Setting
    from sunrise_infrastructure.legal import validate_documents

    configured = load_bootstrap_seed(seed)
    schema, base_url = _scoped_database(settings.database_url)
    if not settings.local_test or settings.instance.visual_profile != "neutral":
        raise ValueError("Подготовка требует нейтрального локального режима.")
    documents = None
    if any(
        (
            settings.legal_offer_url,
            settings.legal_privacy_url,
            settings.legal_offer_version,
            settings.legal_privacy_version,
        )
    ):
        documents = validate_documents(
            [
                {
                    "document_type": "offer",
                    "url": settings.legal_offer_url,
                    "version": settings.legal_offer_version,
                },
                {
                    "document_type": "privacy",
                    "url": settings.legal_privacy_url,
                    "version": settings.legal_privacy_version,
                },
            ]
        )
    base, scoped = Database(base_url), Database(settings.database_url)
    created = False
    try:
        with base.engine.begin() as connection:
            if connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"), {"schema": schema}
            ):
                raise RuntimeError("Схема уже существует; подготовка не перезаписывает экземпляр.")
            connection.execute(CreateSchema(schema))
            created = True
        log_dir = safe_path(ROOT / ".private" / "demo-logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        child_env = clean_environment(os.environ, {"SUNRISE_DATABASE_URL": settings.database_url})
        with safe_path(log_dir / (schema + "-migration.log")).open("wb") as log:
            result = subprocess.run(  # noqa: S603 -- fixed executable/script; credentials only in isolated child environment
                [sys.executable, str(Path(__file__).resolve()), "_migrate"],
                cwd=ROOT,
                env=child_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        if result.returncode:
            raise RuntimeError("Миграция демо не выполнена; журнал сохранён в закрытом каталоге.")
        bootstrap(scoped, seed=configured.model_dump(mode="json"))
        with scoped.transaction() as session:
            if session.execute(text("SELECT current_schema()")).scalar() != schema:
                raise RuntimeError("Изоляция схемы не подтверждена.")
            if documents:
                session.add(Setting(key="legal", value={"documents": documents}))
            session.add(Setting(key="demo_instance", value={"schema": schema, "prepared": True}))
        return {"schema": schema, "prepared": True}
    except Exception:
        if created:
            # Только схема, созданная именно этим вызовом; существующие схемы никогда не удаляются.
            scoped.engine.dispose()
            with base.engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        raise
    finally:
        scoped.engine.dispose()
        base.engine.dispose()


def prepared_database(settings):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from sunrise_infrastructure.db import Database, Setting

    schema, _ = _scoped_database(settings.database_url)
    db = Database(settings.database_url)
    try:
        with db.transaction() as session:
            if session.execute(text("SELECT current_schema()")).scalar() != schema:
                raise RuntimeError("Сначала явно подготовьте схему демо.")
            marker = session.get(Setting, "demo_instance")
            head = ScriptDirectory.from_config(
                Config(str(ROOT / "migrations/alembic.ini"))
            ).get_current_head()
            if (
                not marker
                or marker.value != {"schema": schema, "prepared": True}
                or session.execute(text("SELECT version_num FROM v5_alembic_version")).scalar() != head
            ):
                raise RuntimeError("Подготовленная схема демо не подтверждена.")
    finally:
        db.engine.dispose()


def port_available(port):
    try:
        with socket.socket() as probe4, socket.socket(socket.AF_INET6) as probe6:
            if os.name == "nt":
                probe4.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe6.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe4.bind(("127.0.0.1", port))
            probe6.bind(("::1", port))
        return True
    except OSError:
        return False


def choose_demo_ports(preferred):
    chosen = {}
    for role in ("api", "landing", "dashboard"):
        start = preferred[role]
        for port in range(start, min(start + 256, 65536)):
            if port not in FORBIDDEN_PORTS and port not in chosen.values() and port_available(port):
                chosen[role] = port
                break
        else:
            raise ValueError("Не найдены свободные локальные порты для демо.")
    return chosen


def startup_plan(schema, api_port=8102, landing_port=3102, dashboard_port=3202):
    if not re.fullmatch(r"demo_instance_[a-f0-9]{32}", schema):
        raise ValueError("Нужна собственная UUID-схема демо.")
    ports = [api_port, landing_port, dashboard_port]
    if len(set(ports)) != 3 or any(
        type(port) is not int or not 1024 <= port <= 65535 or port in FORBIDDEN_PORTS for port in ports
    ):
        raise ValueError("Порты демо должны быть отдельными локальными портами.")
    return {
        "schema": schema,
        "ports": {"api": api_port, "landing": landing_port, "dashboard": dashboard_port},
        "urls": {
            "api": f"http://127.0.0.1:{api_port}",
            "landing": f"http://localhost:{landing_port}",
            "dashboard": f"http://localhost:{dashboard_port}",
        },
        "commands": [
            [sys.executable, str(Path(__file__).resolve()), "api"],
            [
                "node",
                str(ROOT / "node_modules/vite/bin/vite.js"),
                "--config",
                "apps/web/landing/vite.config.ts",
                "--host",
                "localhost",
            ],
            [
                "node",
                str(ROOT / "node_modules/vite/bin/vite.js"),
                "--config",
                "apps/web/dashboard/vite.config.ts",
                "--host",
                "localhost",
            ],
        ],
        "web_environment": {
            "WEB_API_BASE_URL": f"http://127.0.0.1:{api_port}",
            "WEB_INSTANCE_ID": "demo-" + schema.removeprefix("demo_instance_"),
        },
    }


def _migrate():
    from alembic import command
    from alembic.config import Config

    _scoped_database(os.environ["SUNRISE_DATABASE_URL"])
    command.upgrade(Config(str(ROOT / "migrations/alembic.ini")), "head")


def stop_children(children):
    for child in children:
        if child.poll() is None:
            if os.name == "nt":
                taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
                subprocess.run(  # noqa: S603 -- only a live Popen child owned by this runner and its descendants
                    [str(taskkill), "/PID", str(child.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=10,
                    check=False,
                )
            if child.poll() is None:
                child.terminate()
    for child in children:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description="Отдельный нейтральный локальный экземпляр")
    parser.add_argument("command", choices=["prepare", "api", "start", "plan", "_migrate"])
    parser.add_argument("--schema")
    parser.add_argument("--seed", default="config/bootstrap.demo.json")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.command == "_migrate":
        _migrate()
        return
    values = demo_values(ROOT / ".env.demo")
    if args.command == "prepare":
        schema = args.schema or "demo_instance_" + uuid4().hex
        initial = startup_plan(schema)
        plan = startup_plan(
            schema, **{key + "_port": value for key, value in choose_demo_ports(initial["ports"]).items()}
        )
        path = ROOT / args.seed
        if (
            Path(args.seed).is_absolute()
            or not path.resolve().is_relative_to(ROOT / "config")
            or path.suffix != ".json"
        ):
            raise ValueError("Начальные данные демо должны находиться в config/.")
        from sunrise_infrastructure.instance_assets import reparse

        if any(reparse(component) for component in [path, *path.parents] if component != ROOT.parent):
            raise ValueError("Ссылки в пути начальных данных запрещены.")
        seed = json.loads(path.read_text(encoding="utf-8"))
        settings = settings_for_demo(values, schema, plan["ports"]["landing"], plan["ports"]["dashboard"])
        prepare_database(settings, seed)
        safe_path(METADATA.parent).mkdir(parents=True, exist_ok=True)
        safe_path(METADATA).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Нейтральный экземпляр подготовлен: " + schema)
        return
    metadata = json.loads(safe_path(METADATA).read_text(encoding="utf-8"))
    plan = startup_plan(
        metadata["schema"], **{key + "_port": value for key, value in metadata["ports"].items()}
    )
    if args.command == "start":
        plan = startup_plan(
            plan["schema"],
            **{key + "_port": value for key, value in choose_demo_ports(plan["ports"]).items()},
        )
        safe_path(METADATA).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    settings = settings_for_demo(values, plan["schema"], plan["ports"]["landing"], plan["ports"]["dashboard"])
    prepared_database(settings)
    if args.command == "plan":
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    if args.command == "api":
        import uvicorn
        from sunrise_api.main import create_app

        uvicorn.run(
            create_app(settings),
            host="127.0.0.1",
            port=plan["ports"]["api"],
            proxy_headers=False,
            access_log=False,
        )
        return
    # Прежде чем запускать детей, проверяем все порты; никогда не останавливаем чужой процесс.
    for port in plan["ports"].values():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        with socket.socket(socket.AF_INET6) as probe6:
            probe6.bind(("::1", port))
    children = []
    log_dir = safe_path(ROOT / ".private/demo-logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        for index, command in enumerate(plan["commands"]):
            additions = dict(values)
            if index:
                additions.update(
                    plan["web_environment"],
                    WEB_DEV_PORT=str(plan["ports"]["landing" if index == 1 else "dashboard"]),
                )
            environment = clean_environment(os.environ, additions)
            with safe_path(log_dir / ("process-" + str(index) + ".log")).open("wb") as log:
                children.append(
                    subprocess.Popen(  # noqa: S603 -- fixed owned application argv, isolated environment and loopback ports
                        command,
                        cwd=ROOT,
                        env=environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                )
        safe_path(METADATA).write_text(
            json.dumps({**plan, "pids": [child.pid for child in children]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("Демо запущено: " + plan["urls"]["dashboard"])
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        raise RuntimeError("Процесс демо завершился; проверьте закрытые журналы.")
    except KeyboardInterrupt:
        pass
    finally:
        stop_children(children)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- CLI boundary masks all errors that could otherwise disclose DSN/credentials
        raise SystemExit(
            "Команда демо не выполнена. Проверьте нейтральную конфигурацию и закрытые журналы."
        ) from None
