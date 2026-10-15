"""Явная композиция изолированного и допущенного production runtime; импорт не запускает polling."""

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from sqlalchemy.engine import make_url

from sunrise_bot.adapter import BotApplication
from sunrise_bot.safety import BotSafety, GuardedSession


@dataclass(frozen=True)
class RuntimeConfig:
    token: str = field(repr=False)
    database_url: str = field(repr=False)
    safety: BotSafety
    custom_emoji_verified: bool = False
    production_settings: object = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env=None):
        values = os.environ if env is None else env
        if values.get("SUNRISE_RUNTIME_MODE") == "production":
            if env is not None:
                raise ValueError("production_environment_override_denied")
            from sunrise_infrastructure.settings import RuntimePolicy, Settings

            from sunrise_bot.safety import ProductionBotSafety

            settings = Settings()
            policy = RuntimePolicy.for_settings(settings)
            return cls(
                settings.bot_token,
                settings.database_url,
                ProductionBotSafety(policy),
                settings.telegram_custom_emoji_verified,
                settings,
            )
        token = values.get("SUNRISE_BOT_TEST_TOKEN", "")
        safety = BotSafety(
            frozenset(filter(None, (x.strip() for x in values.get("SUNRISE_TEST_BOT_IDS", "").split(",")))),
            frozenset(
                filter(
                    None, (x.strip() for x in values.get("SUNRISE_TEST_TELEGRAM_RECIPIENTS", "").split(","))
                )
            ),
        )
        safety.validate(token)
        database_url = values.get("SUNRISE_TEST_DATABASE_URL", "")
        try:
            url = make_url(database_url)
        except Exception as exc:
            raise ValueError("Нужна отдельная синтетическая тестовая база PostgreSQL") from exc
        if (
            url.get_backend_name() != "postgresql"
            or url.host not in {"127.0.0.1", "localhost"}
            or url.port != 55439
            or url.database not in {"sunrise_test", "sunrise_test_backend", "sunrise_test_bot"}
        ):
            raise ValueError(
                "Бот может использовать только локальную базу sunrise_test с синтетическими данными"
            )
        return cls(
            token,
            database_url,
            safety,
            values.get("SUNRISE_TELEGRAM_CUSTOM_EMOJI_VERIFIED", "").lower() == "true",
        )


def compose(config: RuntimeConfig):
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.admin import Admin
    from sunrise_infrastructure.bot_admin import BotAdmin
    from sunrise_infrastructure.commerce import Commerce
    from sunrise_infrastructure.customer_operations import CustomerOperations
    from sunrise_infrastructure.db import Database
    from sunrise_infrastructure.notifications import NotificationService
    from sunrise_infrastructure.panel import configured_panel
    from sunrise_infrastructure.panel_events import PanelEvents
    from sunrise_infrastructure.providers import configured_payments
    from sunrise_infrastructure.security import RateGuard, configured_redis
    from sunrise_infrastructure.settings import Settings, configured_runtime

    settings = config.production_settings or Settings(database_url=config.database_url, local_test=True)
    db = Database(config.database_url)
    policy = configured_runtime(settings, db)
    if policy.production:
        from sunrise_bot.safety import ProductionBotSafety

        safety = ProductionBotSafety(policy)
    else:
        safety = config.safety
    safety.validate(config.token)
    bot = Bot(config.token, session=GuardedSession(AiohttpSession(), safety))
    guard = RateGuard(configured_redis(settings, policy))
    from sunrise_infrastructure.broadcast_media_registry import configured_media_store
    from sunrise_infrastructure.mail import configured_mail

    mail = configured_mail(
        settings,
        media_store=configured_media_store(settings)
        if policy.production or settings.smtp_test_authorized
        else None,
    )
    accounts = Accounts(db, guard, settings, mail_transport=mail)
    commerce = Commerce(
        db,
        local_test=settings.local_test,
        referral_bot_url=settings.bot_url,
        notification_dir=settings.mail_dir + "/notifications",
        telegram_notification_allowlist=config.safety.recipient_ids,
        sandbox_payments=settings.sandbox_payments_enabled,
        panel_provider=configured_panel(settings),
        device_bindings=settings.panel_test_device_bindings,
        owner_id=settings.owner_id,
        public_settings=settings,
        mail_transport=mail,
    )
    admin = Admin(commerce, guard, accounts=accounts)
    console = BotAdmin(admin, settings, telegram_recipient_allowlist=config.safety.recipient_ids)
    admin.bot_console = console
    customer = CustomerOperations(
        commerce,
        PanelEvents(db, commerce.panel_provider, settings, notifications=NotificationService(db, settings)),
    )
    return BotApplication(
        bot,
        commerce,
        safety,
        accounts=accounts,
        admin=admin,
        admin_console=console,
        instance=settings.instance,
        customer_operations=customer,
        custom_emoji=config.custom_emoji_verified,
        payment_provider=configured_payments(settings, policy),
    )


async def run(config: RuntimeConfig):
    app = compose(config)
    owner_alert_task = None
    try:
        identity = await app.bot.get_me()
        if str(identity.id) not in app.safety.bot_ids:
            raise PermissionError("Telegram вернул неразрешённого бота")
        if not app.commerce.scoped_payments:
            owner_alert_task = asyncio.create_task(deliver_pending_owner_alerts(app))
        await app.dispatcher.start_polling(
            app.bot,
            allowed_updates=["message", "callback_query"],
            handle_as_tasks=False,
            close_bot_session=False,
        )
    finally:
        if owner_alert_task:
            owner_alert_task.cancel()
            with suppress(asyncio.CancelledError):
                await owner_alert_task
        await app.bot.session.close()
        app.commerce.db.engine.dispose()


async def deliver_pending_owner_alerts(app):
    while True:
        try:
            await app.deliver_owner_alerts()
        except Exception:  # noqa: BLE001 -- leave durable jobs pending and retry without logging private data
            app.owner_alert_delivery = "retry_pending"
        await asyncio.sleep(15)


def main():
    try:
        config = RuntimeConfig.from_env()
    except ValueError:
        raise SystemExit(
            "Запуск заблокирован: проверьте точный допуск окружения, токен и настройки базы данных."
        ) from None
    asyncio.run(run(config))
