"""Start one guarded test bot, optionally send its actual legal gate to the owner."""

import argparse
import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from sqlalchemy.engine import make_url
from sunrise_bot.runtime import RuntimeConfig, compose, deliver_pending_owner_alerts


async def run(root, greet, admin_preview=False):
    values = dict(dotenv_values(root / ".env", interpolate=False))
    # Both running clients use the common synthetic database, never the private copy.
    values["SUNRISE_TEST_DATABASE_URL"] = values.get("SUNRISE_DATABASE_URL") or values.get(
        "SUNRISE_TEST_DATABASE_URL", ""
    )
    config = RuntimeConfig.from_env(values)
    app = compose(config)
    owner_alert_task = None
    try:
        owner = None
        if greet or admin_preview:
            owner = str(getattr(app.accounts.settings, "owner_id", None) or "")
            if not owner.isdecimal() or int(owner) <= 0 or not config.safety.permits(owner):
                raise PermissionError("Для проверки сообщения нужен явно настроенный разрешённый владелец")
        identity = await app.bot.get_me()
        if str(identity.id) not in config.safety.bot_ids:
            raise PermissionError()
        owner_alert_task = asyncio.create_task(deliver_pending_owner_alerts(app))
        if greet or admin_preview:
            captured = []
            entity_ids = []
            original_show = app.presenter.show

            async def record(*args, **kwargs):
                message = await original_show(*args, **kwargs)
                if message and message.reply_markup:
                    captured.append(message.reply_markup)
                    entity_ids.extend(
                        entity.custom_emoji_id
                        for entity in message.entities or []
                        if entity.type == "custom_emoji"
                    )
                return message

            app.presenter.show = record
            try:
                account = await app.call("resolve_telegram", int(owner))
                if admin_preview:
                    await app.execute(owner, account["id"], "admin", {}, None, "")
                else:
                    await app.main(owner, account)
            finally:
                app.presenter.show = original_show
            rows = captured[-1].inline_keyboard if captured else []
            report = {
                "bot_id": str(identity.id),
                "recipient_id": owner,
                "sent_messages": len(captured),
                "button_rows": [len(row) for row in rows],
                "button_styles": [button.style for row in rows for button in row],
                "button_icon_ids": [button.icon_custom_emoji_id for row in rows for button in row],
                "custom_emoji_entity_ids": entity_ids,
                "custom_emoji_enabled": app.presenter.custom_emoji,
                "client_visual_verified": False,
                "legal_accepted_automatically": False,
                "database": make_url(config.database_url).database,
            }
            filename = "v7-admin-bot-live.json" if admin_preview else "v6-test-bot-live.json"
            (root / ".artifacts").mkdir(exist_ok=True)
            (root / ".artifacts" / filename).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                "Тестовый бот: сообщение разрешённому владельцу доставлено; запускаются обновления",
                flush=True,
            )
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


def main():
    parser = argparse.ArgumentParser(
        description="Локальный запуск отдельного бота с явными списками допуска."
    )
    parser.add_argument("--greet", action="store_true", help="Показать настоящее меню настроенному владельцу")
    parser.add_argument(
        "--admin-preview", action="store_true", help="Показать владельцу разрешённое меню администратора"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    load_dotenv(root / ".env", override=False)
    try:
        asyncio.run(run(root, args.greet, args.admin_preview))
    except Exception as error:  # noqa: BLE001 -- never expose token-bearing transport exceptions
        raise SystemExit("Тестовый бот остановлен: " + type(error).__name__) from None


if __name__ == "__main__":
    main()
