"""Explicit local commands; no auto migrations or bootstrap on API/bot import."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def isolate_test_transports():
    """Test subprocesses never inherit authorization for real external adapters."""
    os.environ["SUNRISE_LOCAL_TEST"] = "true"
    os.environ["SUNRISE_SMTP_TUNNEL_PORT"] = ""
    os.environ["SUNRISE_TURNSTILE_TEST_BYPASS"] = "true"
    os.environ["SUNRISE_TELEGRAM_MINIAPP_ENABLED"] = "false"
    os.environ["SUNRISE_BOT_TEST_TOKEN"] = ""
    for name in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
        os.environ[f"SUNRISE_TELEGRAM_LOGIN_{name}"] = ""
    for service in ("ROLLYPAY", "PANEL", "PANEL_WEBHOOK", "PUSH", "SMTP", "EMOJI", "RECURRING"):
        os.environ[f"SUNRISE_{service}_TEST_AUTHORIZED"] = "false"
    recipients = json.loads(os.environ.get("SUNRISE_EMAIL_ALLOWLIST", "[]"))
    os.environ["SUNRISE_EMAIL_ALLOWLIST"] = json.dumps(
        [email for email in recipients if isinstance(email, str) and email.endswith(".test")]
    )


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    from dotenv import load_dotenv

    load_dotenv(root / ".env", override=False)
    parser = argparse.ArgumentParser(description="Явные команды изолированного локального стенда")
    parser.add_argument("command", choices=["migrate", "bootstrap", "api", "worker", "bot", "test", "check"])
    parser.add_argument(
        "--database",
        choices=["sunrise_test", "sunrise_test_backend", "sunrise_test_bot"],
        default="sunrise_test",
    )
    parser.add_argument("--test-path", choices=["tests", "tests/bot", "tests/review"], default="tests")
    args = parser.parse_args()
    from sunrise_infrastructure.settings import Settings

    settings = Settings()
    if not settings.local_test:
        raise SystemExit("Команды стенда требуют SUNRISE_LOCAL_TEST=true; реальные интеграции запрещены.")
    if args.database != "sunrise_test":
        if args.command not in {"migrate", "bootstrap"}:
            raise SystemExit(
                "Выбор отдельной БД разрешён только для подготовки изолированного тестового набора."
            )
        from sqlalchemy.engine import make_url

        os.environ["SUNRISE_DATABASE_URL"] = (
            make_url(settings.database_url).set(database=args.database).render_as_string(hide_password=False)
        )
        settings = Settings()
    if args.command == "migrate":
        raise SystemExit(
            subprocess.call(
                [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"]
            )
        )
    if args.command == "bootstrap":
        from sunrise_infrastructure.bootstrap_config import bootstrap_instance
        from sunrise_infrastructure.db import Database

        db = Database(settings.database_url)
        try:
            bootstrap_instance(db, settings)
        finally:
            db.engine.dispose()
        print("Начальные тестовые настройки применены без перезаписи существующего каталога.")
    elif args.command == "api":
        import uvicorn

        uvicorn.run(
            "sunrise_api.main:create_app",
            factory=True,
            host="127.0.0.1",
            port=8100,
            proxy_headers=False,
            access_log=False,
        )
    elif args.command == "worker":
        from sunrise_worker.main import main as worker_main

        worker_main()
    elif args.command == "bot":
        from sunrise_bot.runtime import main as bot_main

        os.environ["SUNRISE_TEST_DATABASE_URL"] = settings.database_url
        bot_main()
    elif args.command == "test":
        isolate_test_transports()
        # Интеграции никогда не получают DSN закрытой копии.
        from sqlalchemy.engine import make_url

        os.environ["SUNRISE_TEST_DATABASE_URL"] = (
            make_url(settings.database_url)
            .set(database="sunrise_test_backend")
            .render_as_string(hide_password=False)
        )
        os.environ["SUNRISE_BOT_TEST_DATABASE_URL"] = (
            make_url(settings.database_url)
            .set(database="sunrise_test_bot")
            .render_as_string(hide_password=False)
        )
        os.environ["SUNRISE_TEST_REDIS_URL"] = settings.redis_url
        raise SystemExit(
            subprocess.call(  # noqa: S603 -- fixed pytest executable/argv; test path constrained by argparse choices
                [sys.executable, "-m", "pytest", args.test_path, "--tb=short"]
            )
        )
    elif args.command == "check":
        raise SystemExit(subprocess.call([sys.executable, "scripts/check_architecture.py"]))


if __name__ == "__main__":
    main()
