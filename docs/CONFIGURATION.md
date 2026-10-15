# Конфигурация

[Установка](INSTALL_RU.md) · [Развёртывание](DEPLOYMENT.md) · [Решение проблем](TROUBLESHOOTING.md)

Настройки сервера определяет [Settings](../packages/infrastructure/src/sunrise_infrastructure/settings.py), оформление определяет [InstanceConfig](../packages/infrastructure/src/sunrise_infrastructure/instance_config.py). `.env.example` является примером изолированного стенда. Не используйте его как готовую production-конфигурацию.

## Файлы и приоритет

Рабочая директория команд ниже: корень приложения. Сервер читает `.env`; уже заданные переменные процесса могут иметь приоритет. `INSTANCE_CONFIG_PATH` выбирает JSON экземпляра, по умолчанию `config/instance.json`. Скопируйте [пример](../config/instance.example.json) в этот файл. Непустые переменные оформления переопределяют значения файла. После изменений перезапустите затронутые процессы.

Храните `.env`, admission-файлы и ключи вне публикуемого состава, с доступом только оператора и нужного процесса. В Unix используйте права `0600` для секретных файлов. В Windows ограничьте ACL своей учётной записью и сервисом. Значения паролей не передавайте в issue, снимках, командной строке или выводе `docker compose config`.

## Базовые параметры

| Переменная | Значение по умолчанию / требование |
| --- | --- |
| `SUNRISE_DATABASE_URL` | Обязательна. SQLAlchemy DSN `postgresql+psycopg`; в isolated разрешены только loopback, порт `55439` и базы `sunrise_test`, `sunrise_test_backend`, `sunrise_test_bot` |
| `SUNRISE_REDIS_URL` | `redis://127.0.0.1:56379/0`; пароль собственного Redis нужен в DSN |
| `SUNRISE_SECRET_KEY` | Обязательна, минимум 32 символа. Генерируется отдельно для экземпляра |
| `SUNRISE_RUNTIME_MODE` | `isolated`; также существуют `scoped_test` и `production`, требующие своих admission-проверок |
| `SUNRISE_LOCAL_TEST` | `false`; локальная инструкция явно задаёт `true` |
| `SUNRISE_ALLOWED_HOSTS` | JSON `["127.0.0.1", "localhost"]` для локального стенда; публичный runtime требует точные имена обоих доменов |
| `SUNRISE_ALLOWED_ORIGINS` | JSON адресов кабинета на порту `3200`; wildcard запрещён |
| `SUNRISE_COOKIE_SECURE` | `false` для локального HTTP; публичная установка требует HTTPS и `true` |
| `SUNRISE_MAIL_DIR` | `.private/mail`; локальный почтовый sink содержит одноразовые коды и должен оставаться приватным |

DSN содержит пароль: экранируйте специальные символы по правилам URL. Генератор локальной установки использует собственные случайные значения. Не подставляйте туда пароль из другой установки.

## Совместимость технических имён

Название нейтрального проекта: VLESS VPN Shop. Имена Python-пакетов `sunrise_*`, совместимый префикс переменных `SUNRISE_*` и имена разрешённых тестовых баз являются техническими идентификаторами. Их переименование для смены бренда не требуется. Используйте `APP_NAME` и конфигурацию экземпляра; сохраняйте имена параметров из примеров и команд.

## Оформление и адреса

| Настройка | Назначение |
| --- | --- |
| `APP_NAME`, `APP_SHORT_NAME`, `APP_SLUG`, `APP_DESCRIPTION` | Название, короткое имя, технический slug, описание |
| `VISUAL_PROFILE`, `ACCENT_COLOR` | Нейтральный профиль и цвет оформления |
| `PUBLIC_SITE_URL`, `DASHBOARD_URL` | Адреса лендинга и кабинета; локальный пример использует `3100` и `3200` |
| `TELEGRAM_BOT_USERNAME`, `SUPPORT_URL`, `SUPPORT_EMAIL`, `SENDER_NAME` | Бот, поддержка и имя отправителя |
| `INSTANCE_ASSET_ROOT` | Корень файлов экземпляра |
| `LOGO_ASSET_PATH`, `FAVICON_ASSET_PATH`, `HERO_ASSET_PATH`, `OG_IMAGE_ASSET_PATH` | Необязательные изображения внутри разрешённого корня |
| `SUBSCRIPTION_PUBLIC_ORIGIN` | Необязательный публичный origin ссылок подписки; для внешнего адреса используйте собственный HTTPS-host |

В JSON используются поля `app_name`, `public_site_url`, `dashboard_url`, `asset_root`, `logo_asset` и другие из примера. При отсутствии изображений работает текстовый вариант. Цены и тарифы в файл оформления не входят.

## Вход, администратор и юридические документы

`SUNRISE_EMAIL_ALLOWLIST` является JSON-списком разрешённых тестовых адресов. Локальный sink принимает синтетические адреса `.test`. Пустой список запрещает доставку. `SUNRISE_TEST_TELEGRAM_RECIPIENTS` и `SUNRISE_TEST_BOT_IDS` содержат ID строками через запятую; пустое значение запрещает соответствующие тестовые действия.

`SUNRISE_OWNER_ID` содержит реальный проверенный Telegram ID владельца. `SUNRISE_ADMIN_TELEGRAM_ALLOWLIST` является JSON-списком проверенных Telegram ID строками. Почтовый allowlist администратора сам по себе не выдаёт права. Свежий подписанный Telegram-вход, роли и MFA проверяются дополнительно. [Локальный bootstrap владельца](INSTALL_RU.md#администратор) ограничен тестовой базой и не является production-командой.

Для OIDC задаются `SUNRISE_TELEGRAM_LOGIN_CLIENT_ID`, `SUNRISE_TELEGRAM_LOGIN_CLIENT_SECRET`, `SUNRISE_TELEGRAM_LOGIN_REDIRECT_URI`. Mini App отдельно включает `SUNRISE_TELEGRAM_MINIAPP_ENABLED` и требует соответствующего токена бота для проверки подписанных данных. `SUNRISE_BOT_TEST_TOKEN` предназначен для отдельного тестового бота; `SUNRISE_BOT_TOKEN` используется production-контуром с admission. Регистрация redirect URI и Mini App у Telegram выполняется владельцем своего бота.

`OFFER_URL`, `PRIVACY_URL`, `OFFER_VERSION`, `PRIVACY_VERSION` задают начальные юридические ссылки и версии. Общий `bootstrap_instance` вызывается из `run_local.py bootstrap`, Docker init и production init: он проверяет полный комплект и сохраняет документы только при отсутствии настроенных документов. Существующие документы и согласия не перезаписываются. Полностью пустой комплект оставляет документы ненастроенными, частичный или некорректный даёт отказ. API при обычном старте bootstrap не выполняет. После первичного заполнения база и административный контент являются источником действующих документов. Опубликуйте свои тексты и корректные версии перед покупками; не выдавайте примерные тексты за юридические документы оператора. Turnstile использует `SUNRISE_TURNSTILE_SITE_KEY`, `SUNRISE_TURNSTILE_SECRET`, `SUNRISE_TURNSTILE_HOSTNAME`; `SUNRISE_TURNSTILE_TEST_BYPASS` допустим только для изолированной локальной проверки.

## Провайдеры

| Группа | Параметры и ограничения |
| --- | --- |
| RollyPay | `SUNRISE_ROLLYPAY_API_KEY`, `SUNRISE_ROLLYPAY_SIGNING_SECRET`, `SUNRISE_ROLLYPAY_TERMINAL`, `SUNRISE_ROLLYPAY_BASE_URL` (по умолчанию `https://rollypay.io`), `SUNRISE_PAYMENT_ALLOWED_HOSTS` |
| Webhook | Для сгенерированного runtime `SUNRISE_WEBHOOK_BASE_URL` должен совпадать с HTTPS origin кабинета, например `https://cabinet.example.test`, без пути, query и секретов |
| Remnawave | `SUNRISE_PANEL_CONTRACT`, `SUNRISE_PANEL_BASE_URL`, `SUNRISE_PANEL_TOKEN`, `SUNRISE_PANEL_ALLOWED_HOSTS`, `SUNRISE_PANEL_SUBSCRIPTION_ALLOWED_HOSTS`, `SUNRISE_PANEL_WEBHOOK_SECRET` |
| SMTP | `SUNRISE_SMTP_HOST`, `SUNRISE_SMTP_PORT` (`587`), `SUNRISE_SMTP_MODE` (`starttls`, также `smtps`), `SUNRISE_SMTP_SENDER`, `SUNRISE_SMTP_USERNAME`, `SUNRISE_SMTP_PASSWORD`, `SUNRISE_SMTP_ALLOWED_HOSTS` |
| Web Push | `SUNRISE_PUSH_PRIVATE_KEY`, `SUNRISE_PUSH_SUBJECT`; настройка браузера также требует разрешения пользователя |

Сгенерированный production bundle обслуживает два host: лендинг и кабинет.
Задайте `SUNRISE_WEBHOOK_BASE_URL=https://cabinet.example.test`, где имя совпадает
с вашим `DASHBOARD_URL`. В настройках провайдеров указывайте полные endpoint:
`https://cabinet.example.test/api/v1/webhooks/rollypay` и
`https://cabinet.example.test/api/v1/webhooks/remnawave`.
Короткие пути `/rollypay` и `/remnawave` не являются документированными callback
bundle; вспомогательный `Settings.webhook_url` не строит эти рабочие адреса.
Третий webhook-host renderer не обслуживает. Это дополнительная проверка
генератора runtime, а не изменение общего контракта `Settings` для других установок.

Тестовые возможности RollyPay, панели, webhook панели, SMTP, Push, emoji и recurring по умолчанию выключены отдельными `SUNRISE_*_TEST_AUTHORIZED`. Включение требует собственных разрешённых ресурсов и получателей; поле не заменяет проверку ownership. Полный набор точных имён и типов находится в `Settings` и `.env.example`.

Сверка комиссии известных платежей использует `SUNRISE_PAYMENT_SETTLEMENT_INTERVAL_SECONDS` (`300`, диапазон `60..86400`) и `SUNRISE_PAYMENT_SETTLEMENT_BATCH_SIZE` (`10`, диапазон `1..50`). Неизвестная комиссия не равна нулю. Новое live-подключение автопродления заблокировано кодом независимо от этих настроек.

## Каталог и production admission

[bootstrap.example.json](../config/bootstrap.example.json) содержит пример каталога и политики скидок. Явный bootstrap заполняет новый пустой каталог. Если в базе уже есть хотя бы один продукт, начальное заполнение каталога и его настроек пропускается: частично заполненный каталог автоматически не дополняется. После этого владелец управляет каталогом через админку. Примерные цены не являются рекомендованным прайсом.

Production использует `SUNRISE_PRODUCTION_ADMISSION_FILE` и `SUNRISE_PRODUCTION_ADMISSION_SHA256`, проверенные ресурсы, хеши файлов и согласованные записи базы `production_admission` / `production_runtime_gates`. Включение `SUNRISE_RUNTIME_MODE=production` без этого набора завершится отказом.

`scripts/install_production_instance.py` подготавливает только новый экземпляр на пустой проверенной базе. Этап `admit` создаёт admission с выключенными максимальными возможностями и runtime gates. Отдельная первая активация требует явного выбора возможностей, подтверждения scope и хешей, реальных проверок владельца и отсутствия пользовательской активности. Команды, требования и ограничения приведены в [DEPLOYMENT.md](DEPLOYMENT.md). Эти этапы проверены на отдельной настоящей PostgreSQL с синтетическими данными и разрешением только `auth_mail`; проверены также откат транзакции и конкурентные блокировки. Это не проверка реальной почтовой доставки или оплаты.

`scripts/render_production_runtime.py` использует закреплённые планы и действующие TLS-файлы для подготовки HTTPS Compose-bundle. Нужны два разных домена из instance config и внешние собственные PostgreSQL/Redis, доступные из bridge-сети; loopback DSN не подходит. Renderer не меняет исходные секреты, не проверяет БД или внешние провайдеры и не запускает сервисы. В bundle сохраняются приватные реквизиты, поэтому он остаётся под `.private/production/`.
