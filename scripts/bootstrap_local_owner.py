"""Явное offline-назначение первого владельца только локального тестового экземпляра."""

import argparse
import json
import re
import sys
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Account, Alias, Audit, Database, Setting, StaffRole
from sunrise_infrastructure.staff_permissions import telegram_admin_allowed

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASES = {"sunrise_test", "sunrise_test_backend", "sunrise_test_bot"}


def validate_target(settings):
    try:
        parsed = make_url(settings.database_url)
        options = parsed.query.get("options", "")
        schema = options.removeprefix("-csearch_path=") if options else "public"
        if (
            settings.local_test is not True
            or parsed.drivername != "postgresql+psycopg"
            or parsed.host not in {"127.0.0.1", "localhost"}
            or parsed.port != 55439
            or parsed.database not in TEST_DATABASES
            or dict(parsed.query) not in ({}, {"options": "-csearch_path=" + schema})
            or schema != "public"
            and not re.fullmatch(r"demo_instance_[a-f0-9]{32}", schema)
            or schema != "public"
            and parsed.database != "sunrise_test_backend"
        ):
            raise ValueError
        return schema
    except (ArgumentError, ValueError, TypeError, AttributeError):
        raise DomainError(
            "owner_target_denied", "Назначение разрешено только в точной локальной тестовой БД/схеме.", 400
        ) from None


def validate_confirmation(account_id, email, confirm_email, confirmed, reason):
    if (
        not isinstance(account_id, str)
        or not re.fullmatch(r"[1-9][0-9]{0,18}", account_id)
        or int(account_id) > 2**63 - 1
        or not isinstance(email, str)
        or len(email) > 320
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        or not isinstance(confirm_email, str)
        or email.casefold() != confirm_email.casefold()
        or confirmed is not True
        or not isinstance(reason, str)
        or not 5 <= len(reason.strip()) <= 500
    ):
        raise DomainError(
            "owner_confirmation_required",
            "Укажите канонический ID, собственный подтверждённый email, повтор email, причину и явное подтверждение.",
            400,
        )
    return int(account_id), email.casefold()


def grant_first_owner(db, settings, *, account_id, email, confirm_email, confirmed, reason):
    identifier, normalized = validate_confirmation(account_id, email, confirm_email, confirmed, reason)
    schema = validate_target(settings)
    # An injected connection must have the same bounded target, not merely safe settings.
    if make_url(settings.database_url) != db.engine.url:
        raise DomainError("owner_target_denied", "Подключение не соответствует выбранному экземпляру.", 400)
    with db.transaction() as session:
        if (
            session.execute(text("SELECT current_schema()")).scalar() != schema
            or session.execute(text("SELECT current_database()")).scalar()
            != make_url(settings.database_url).database
        ):
            raise DomainError(
                "owner_target_denied", "Подключение не соответствует выбранной тестовой области.", 400
            )
        if schema != "public":
            marker = session.get(Setting, "demo_instance")
            if not marker or marker.value != {"schema": schema, "prepared": True}:
                raise DomainError(
                    "owner_target_denied", "Сначала явно подготовьте собственную схему demo.", 400
                )
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('sunrise:local-owner-bootstrap'))"))
        account = session.scalar(
            select(Account)
            .where(Account.id == identifier)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not account
            or session.get(Alias, identifier)
            or not telegram_admin_allowed(account, settings)
            or not account.email_verified
            or not account.email
            or account.email.casefold() != normalized
            or getattr(settings, "owner_id", None) is not None
            and str(account.telegram_id) != str(settings.owner_id)
        ):
            raise DomainError(
                "owner_identity_denied",
                "Требуется канонический аккаунт с разрешённым Telegram и совпадающей подтверждённой почтой; настройки владельца должны совпадать.",
                409,
            )
        owners = list(session.scalars(select(StaffRole).where(StaffRole.role == "owner").with_for_update()))
        if any(row.account_id != identifier for row in owners):
            raise DomainError(
                "owner_already_configured",
                "Другой владелец уже назначен. Эта команда не заменяет владельцев.",
                409,
            )
        existing = session.scalar(
            select(StaffRole).where(StaffRole.account_id == identifier).with_for_update()
        )
        if existing:
            if existing.role == "owner":
                return {"status": "already_owner", "account_id": account_id}
            raise DomainError(
                "owner_role_conflict",
                "У аккаунта уже есть сохранённая роль. Первоначальная настройка её не изменяет.",
                409,
            )
        session.add(StaffRole(account_id=identifier, role="owner", granted_by=identifier))
        session.add(
            Audit(
                actor_id=identifier,
                action="local_owner_bootstrap",
                target="account:" + account_id,
                reason=reason.strip(),
                details={
                    "before": None,
                    "after": {"role": "owner", "account_id": account_id},
                    "source": "offline_local_cli",
                    "schema": schema,
                },
            )
        )
        return {"status": "granted", "account_id": account_id}


def load_settings(profile):
    if __package__:
        from . import run_demo_instance as demo
    else:
        import run_demo_instance as demo
    if profile == "demo":
        values = demo.demo_values(ROOT / ".env.demo")
        metadata = json.loads(demo.safe_path(demo.METADATA).read_text(encoding="utf-8"))
        settings = demo.settings_for_demo(
            values, metadata["schema"], metadata["ports"]["landing"], metadata["ports"]["dashboard"]
        )
        validate_target(settings)
        demo.prepared_database(settings)
        return settings
    from dotenv import load_dotenv
    from sunrise_infrastructure.settings import Settings

    path = demo.safe_path(ROOT / ".env")
    if path.is_file() and path.stat().st_size <= 65536:
        load_dotenv(path, override=False)
    else:
        raise DomainError("owner_target_denied", "Подготовьте собственный ограниченный локальный .env.", 400)
    settings = Settings(_env_file=None)
    validate_target(settings)
    return settings


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise DomainError("owner_arguments_invalid", "Проверьте параметры команды; используйте --help.", 400)


def main(argv=None):
    parser = SafeParser(description="Явное назначение первого владельца локального тестового экземпляра")
    parser.add_argument("--profile", choices=["local", "demo"], default="local")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--confirm-email")
    parser.add_argument("--confirm-first-owner", action="store_true")
    parser.add_argument("--reason", required=True)
    db = None
    try:
        args = parser.parse_args(argv)
        validate_confirmation(
            args.account_id, args.email, args.confirm_email, args.confirm_first_owner, args.reason
        )
        settings = load_settings(args.profile)
        db = Database(settings.database_url)
        result = grant_first_owner(
            db,
            settings,
            account_id=args.account_id,
            email=args.email,
            confirm_email=args.confirm_email,
            confirmed=args.confirm_first_owner,
            reason=args.reason,
        )
        print(
            "Владелец уже был назначен; изменений нет."
            if result["status"] == "already_owner"
            else "Первый владелец локального экземпляра назначен; запись аудита сохранена."
        )
        return 0
    except DomainError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - CLI must not disclose DSN, credentials or raw DB/config errors
        print(
            "Назначение не выполнено. Проверьте локальную конфигурацию, подготовку БД и параметры без публикации секретов.",
            file=sys.stderr,
        )
        return 2
    finally:
        if db is not None:
            db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
