# Установка

[Русский](INSTALL_RU.md) · [English](INSTALL_EN.md) · [README](../README.md)

Инструкция запускает отдельный синтетический стенд на localhost. Чистые Docker-сборки backend и web, миграции до `v11_0019`, PostgreSQL, Redis и worker проверены в изолированном окружении с подменами внешних провайдеров. Доступ с Windows-хоста через локальный Nginx ingress, защита закрытых маршрутов и запрет внешнего выхода приложения проверены. В [DEPLOYMENT.md](DEPLOYMENT.md) описан отдельный оператор новой production-базы: его этапы и первая активация проверены на отдельной настоящей PostgreSQL с синтетическими данными и разрешением только почтовой аутентификации; реальные провайдеры не вызывались.

Для Linux production предусмотрен отдельный генератор HTTPS Compose-bundle со статическим frontend, собственными внешними PostgreSQL/Redis и TLS-файлами. Порядок подготовки, read-only проверки и последующего запуска находится в [DEPLOYMENT.md](DEPLOYMENT.md#https-runtime-bundle). Проверены 20 сценариев настоящего Nginx/HTTPS с собственным тестовым CA и проверка ограничения webhook origin доменом кабинета.

Для bundle задайте `SUNRISE_WEBHOOK_BASE_URL` равным HTTPS origin кабинета,
например `https://cabinet.example.test`. Полные адреса у провайдеров:
`https://cabinet.example.test/api/v1/webhooks/rollypay` и
`https://cabinet.example.test/api/v1/webhooks/remnawave`. Отдельный третий
webhook-host этим bundle не обслуживается.

## Требования

Нужны Python 3.12 или новее и Docker с Compose v2 для Linux-контейнеров. Для разработки frontend вне Docker нужен Node.js 24 и npm. Образы стенда используют Python 3.12, Node.js 24, PostgreSQL 18.4 и Redis 8.2.3.

В Windows установите Python с launcher `py`, включите необходимые компоненты виртуализации/WSL и запустите Docker Desktop в режиме Linux-контейнеров. Если установка WSL или Windows-компонентов требует перезагрузки, выполните её до проверки движка. В Linux установите Docker Engine и Compose plugin штатным способом своей системы. Избегайте непроверенных скриптов установки.

Проверьте:

```text
docker version
docker compose version
```

`docker version` должен показывать клиент и сервер. Рабочий клиент без секции Server недостаточен для сборки.

## Получить исходники

Распакуйте полученный архив в отдельный каталог или клонируйте адрес, предоставленный владельцем опубликованного репозитория. Удалённый репозиторий этой инструкцией не создаётся. Все следующие команды выполняются из каталога с `pyproject.toml` и `package.json`.

## Docker: подготовка и запуск

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe scripts/setup_local_docker.py prepare
docker compose --env-file .env.docker -f infra/compose.standalone.json build
docker compose --env-file .env.docker -f infra/compose.standalone.json up -d
docker compose --env-file .env.docker -f infra/compose.standalone.json ps -a
```

Linux/macOS:

```bash
python3.12 -m venv .venv
.venv/bin/python scripts/setup_local_docker.py prepare
docker compose --env-file .env.docker -f infra/compose.standalone.json build
docker compose --env-file .env.docker -f infra/compose.standalone.json up -d
docker compose --env-file .env.docker -f infra/compose.standalone.json ps -a
```

Подготовка использует стандартную библиотеку Python, установка зависимостей на хост для неё не нужна. Скрипт создаёт игнорируемый `.env.docker` со случайными секретами и выводит только адреса интерфейсов. Существующий файл сохраняется. Не публикуйте его и не копируйте в Git.

По умолчанию лендинг получает `http://127.0.0.1:3100`, кабинет `http://127.0.0.1:3200`. Если порт занят, скрипт выбирает свободный и печатает фактический адрес. Для фиксированных портов на первом запуске добавьте `--landing-port 33100 --dashboard-port 33200`; явно занятый порт даст отказ.

Compose запускает PostgreSQL, Redis, одноразовый `init`, API, worker, два web-интерфейса и отдельный Nginx `ingress`. `init` последовательно выполняет защищённые миграции и начальное заполнение каталога. API стартует только после успешного `init`; worker и web ждут API. Успешный `init` имеет состояние завершения с кодом `0`.

Только Nginx `ingress` публикует web-порты на `127.0.0.1`. Он работает под UID `101`, без секретных env-файлов и с двумя фиксированными upstream. Приложение, база и Redis остаются во внутренней сети без внешнего выхода; прикладные сервисы используют общее сетевое пространство контейнера PostgreSQL. База, Redis и API на хост не публикуются. Загрузка образов и зависимостей при сборке требует интернета.

Начальный профиль отключает внешние провайдеры, Telegram polling и получателей. Поэтому каталог можно проверить, а внешние платежи и доставка сообщений не выполняются. Пустой почтовый allowlist также блокирует получение кода входа. Для сценариев входа нужен отдельно настроенный синтетический профиль, описанный ниже.

Остановка с сохранением данных:

```text
docker compose --env-file .env.docker -f infra/compose.standalone.json stop
```

Повторный запуск использует ту же команду `up -d`. Не применяйте `down -v`: этот флаг удаляет тома данных.

## Разработка вне контейнеров

Этот вариант требует отдельно работающих PostgreSQL и Redis. Сначала скопируйте `.env.example` в новый `.env`, а `config/instance.example.json` в `config/instance.json`. Существующие рабочие файлы не перезаписывайте. Укажите собственные `LOCAL_POSTGRES_PASSWORD` и `LOCAL_REDIS_PASSWORD` в защищённом `.env`. [compose.local.yml](../infra/compose.local.yml) публикует сервисы только на loopback-портах `55439` и `56379`; запуск выполняется так:

```text
docker compose --env-file .env -f infra/compose.local.yml up -d
```

Заполните DSN базы и Redis с теми же новыми локальными паролями, `SUNRISE_SECRET_KEY` случайным секретом длиной не менее 32 символов, `SUNRISE_LOCAL_TEST=true`. Для входа задайте только свои синтетические адреса `.test` в `SUNRISE_EMAIL_ALLOWLIST`, например `["developer@example.test"]`, и локальный `SUNRISE_MAIL_DIR=.private/mail`. Установите `SUNRISE_TURNSTILE_TEST_BYPASS=true`. Остальные внешние разрешения оставьте выключенными.

До команды `bootstrap` заполните все четыре юридических параметра: `OFFER_URL=https://example.test/offer`, `PRIVACY_URL=https://example.test/privacy`, `OFFER_VERSION=local-v1`, `PRIVACY_VERSION=local-v1`. Это синтетические примеры; сетевое обращение к этим страницам не требуется. `run_local.py bootstrap` проверяет комплект и сохраняет начальные документы в базе, если они ещё не настроены. Существующие документы и согласия сохраняются. Частичный или некорректный комплект даёт отказ; полностью пустой оставляет документы ненастроенными и вход заблокированным. Запуск API сам по себе bootstrap не выполняет. Проверка всего сценария входа на настоящем PostgreSQL ещё не выполнена. Для публичной установки нужны собственные действующие документы. Подробности: [CONFIGURATION.md](CONFIGURATION.md).

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install pip==26.2.1 setuptools==83.0.0 wheel==0.46.2
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
npm.cmd ci
.\.venv\Scripts\python.exe scripts/run_local.py migrate
.\.venv\Scripts\python.exe scripts/run_local.py bootstrap
.\.venv\Scripts\python.exe scripts/run_local.py api
```

В отдельных терминалах из того же каталога:

```powershell
.\.venv\Scripts\python.exe scripts/run_local.py worker
npm.cmd run dev:landing
npm.cmd run dev:dashboard
```

Последние три команды запускаются каждая в своём терминале. Linux/macOS используют `.venv/bin/python` вместо `.\.venv\Scripts\python.exe` и `npm` вместо `npm.cmd` с теми же аргументами. Активация venv не обязательна; явный путь предотвращает выбор другого Python. API слушает `127.0.0.1:8100`; health endpoint: `/api/v1/health`.

Почтовый sink хранит тестовый код локально. Открывайте только собственные сообщения в `.private/mail`; не прикладывайте коды к отчётам. Удалённый SMTP для локального входа не нужен.

## Администратор

Обычный email-вход создаёт клиентский аккаунт. Для администратора требуется канонический аккаунт с проверенной разрешённой Telegram-идентичностью, настроенными `SUNRISE_OWNER_ID` / `SUNRISE_ADMIN_TELEGRAM_ALLOWLIST` и MFA. Введение Telegram ID в форму или выдача роли по почте не заменяет проверку личности.

Совпадение проверенного Telegram ID с `SUNRISE_OWNER_ID` даёт роль владельца без отдельного offline-назначения. Настройка собственного Telegram Login клиента, redirect URI и первого входа с MFA описана в [DEPLOYMENT.md](DEPLOYMENT.md#первый-вход-владельца). Настоящий OIDC-вход в этой подготовке не проверялся.

`scripts/bootstrap_local_owner.py --help` описывает ограниченный offline-bootstrap первого владельца. Он работает только с разрешённой локальной тестовой базой, существующим подтверждённым аккаунтом и повторным подтверждением email, записывает аудит и не заменяет другого владельца. В Docker-профиле по умолчанию тестовая Telegram-аутентификация не настроена; административный вход нельзя считать готовым сразу после `up`.

Перед покупками настройте свои юридические ссылки и версии, тарифы и Remnawave-сопоставления. Сведения о начальных настройках и дальнейшей правке через админку: [CONFIGURATION.md](CONFIGURATION.md).

## Проверки

Windows:

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
.\.venv\Scripts\python.exe scripts/check_architecture.py
.\.venv\Scripts\python.exe -m pytest tests/unit --tb=short
```

Для PostgreSQL-наборов подготовьте отдельные синтетические базы `sunrise_test_backend` и `sunrise_test_bot`, затем задайте соответствующие тестовые DSN. `scripts/run_isolated_tests.py` проверяет назначение и создаёт временные схемы. Общие PostgreSQL-наборы запускайте последовательно. Пропущенный тест из-за отсутствия сервиса не является PASS интеграции.

[Решение проблем](TROUBLESHOOTING.md) · [Обновление](UPDATING.md) · [Резервные копии](BACKUP_RESTORE.md)
