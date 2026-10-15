# Развёртывание

[Локальная установка](INSTALL_RU.md) · [Конфигурация](CONFIGURATION.md) · [Резервные копии](BACKUP_RESTORE.md)

Эта редакция содержит синтетический localhost-стенд и отдельный оператор подготовки нового production-экземпляра. Проверены чистые Docker-сборки, PostgreSQL/Redis, миграции, worker, localhost ingress, сетевые ограничения и поэтапная подготовка отдельной настоящей PostgreSQL с синтетическими данными. Первичная подготовка оставляет все возможности выключенными. Первая активация проверена только для `auth_mail`, без реальной доставки. Проверка ограничения webhook origin доменом кабинета пройдена.

## Локальный стенд

Следуйте [INSTALL_RU.md](INSTALL_RU.md). API и worker используют одну базу. Бот является отдельным необязательным процессом; для внешнего тестирования нужен собственный тестовый бот и явные ограничения получателей. Локальный HTTP и почтовый sink подходят для проверки интерфейса и бизнес-логики на синтетических данных.

Публикация локальных портов в интернет не превращает стенд в production. Не подставляйте реальные данные, production-токены или клиентскую базу в локальный профиль.

## Что требуется для публичной установки

Оператор должен подготовить собственные домены лендинга и кабинета, HTTPS, закрытые PostgreSQL и Redis, защищённые конфигурацию и резервные копии. API кабинета следует проксировать под тем же origin. Лендинг не должен получать сессионные cookies кабинета. Настройте разрешённые hosts/origins без wildcard, secure cookies и точные redirect URI Telegram.

До включения интеграций проверьте токен и идентичность собственного бота, API и webhook-секреты панели и платёжного провайдера, SMTP с шифрованием, юридические документы, каталог и права владельца. Секрет подписи callback и API key платежей имеют разные назначения.

Для этого двухдоменного bundle `SUNRISE_WEBHOOK_BASE_URL` должен совпадать с HTTPS
origin кабинета без пути, например `https://cabinet.example.test`.
Укажите у RollyPay полный callback
`https://cabinet.example.test/api/v1/webhooks/rollypay`, а у Remnawave
`https://cabinet.example.test/api/v1/webhooks/remnawave`.
Замените пример на свой host. Renderer отклоняет отдельный третий webhook-host;
его Nginx такого host не обслуживает. Короткие пути из `Settings.webhook_url`
не подставляются вместо этих endpoint. Ограничение относится к сгенерированному
runtime и не меняет общие настройки других установок.

Проверьте маршрут callback целиком: публичный endpoint, проверка подписи, внутреннее соединение с API и обработка inbox. Отказ на неподписанном запросе проверяет только внешний участок. Проверка внутренней доставки должна сохранять идемпотентность и не создавать фиктивную оплату.

## Защитные проверки допуска

`SUNRISE_RUNTIME_MODE=production` требует admission-файл с совпадающим SHA-256. [Settings](../packages/infrastructure/src/sunrise_infrastructure/settings.py) сверяет адреса и идентичности БД, Redis, бота, панели и платёжного контура. Runtime также проверяет конфигурацию экземпляра, манифест исходников и сохранённые в базе разрешения возможностей.

Допуск разделяет создание счетов, сверку платежей, финансовое применение, операции панели, доставку уведомлений, рассылки и другие возможности. Ошибка допуска останавливает зависимые операции. Не удаляйте проверки и не заменяйте их константой для запуска на другом сервере.

Обычный Alembic entrypoint ограничен изолированными loopback-базами. Отдельный оператор ниже выполняет зафиксированную цепочку миграций только для новой пустой базы. `scripts/bootstrap_local_owner.py` намеренно не выдаёт production-права.

## Подготовка нового production-экземпляра

[install_production_instance.py](../scripts/install_production_instance.py) выполняет этапы `plan`, `init`, `admit`, `verify`. Он не обновляет существующую установку и не включает покупки, рассылки, уведомления или обращения к панели. Максимальные capabilities и runtime gates создаются со всеми значениями `false`.

До запуска подготовьте отдельную пустую PostgreSQL-базу и выбранную схему. Подключаемая роль должна быть без superuser и владеть базой и этой схемой; её `search_path` должен точно совпадать с выбранной схемой, например `shop`, без `"$user", public`. DSN должен содержать явный порт и не содержать query-параметров. Чужие схемы, существующие прикладные данные и DDL-триггеры приводят к отказу.

Создайте приватный `.env.production` с полными настройками своего экземпляра: `INSTANCE_CONFIG_PATH=config/instance.json`, `SUNRISE_RUNTIME_MODE=production`, `SUNRISE_COOKIE_SECURE=true`, точные DSN, origins, host-списки и требуемые реквизиты провайдеров. Все тестовые флаги должны быть `false`. Исходный файл не должен содержать ранее созданные ключи admission. Секреты и настройку доменов оператор не получает автоматически.

Скопируйте [production-scope.example.json](../config/production-scope.example.json) в `.private/production/install-input.json`. Заполните проверенные UUID squads и узлов панели, разрешённые subscription hosts, схему базы и Redis-префикс своей установки. Пустой `internal_squads` в примере специально не проходит проверку. Не используйте ресурсы чужой установки.

В подготовленном Python-окружении выполните `plan`. Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts/install_production_instance.py plan --env-file .env.production --scope-file .private/production/install-input.json
```

`plan` читает базу без изменения её данных, проверяет настройки и сохраняет приватный план. Запишите возвращённый `plan_sha256`. Просмотрите назначение до следующего этапа. В командах ниже замените `SHA256_FROM_PLAN` на этот точный хеш:

```powershell
.\.venv\Scripts\python.exe scripts/install_production_instance.py init --expected-plan-sha256 SHA256_FROM_PLAN
.\.venv\Scripts\python.exe scripts/install_production_instance.py admit --expected-plan-sha256 SHA256_FROM_PLAN
.\.venv\Scripts\python.exe scripts/install_production_instance.py verify --expected-plan-sha256 SHA256_FROM_PLAN
```

Linux/macOS используют `.venv/bin/python` с теми же аргументами. `--env-file` и `--scope-file` принимаются только командой `plan`; последующие команды используют зафиксированные входы.

`init` применяет цепочку из 19 миграций и seed к пустой базе, затем сохраняет receipt. Общий bootstrap предварительно проверяет юридические параметры и сохраняет начальные документы при их отсутствии; существующие документы и согласия не изменяет. Полностью пустой комплект оставляет документы ненастроенными, частичный даёт отказ. `admit` формирует манифест исходников, admission, закрытый `.private/production/runtime.env` и согласованные записи допуска в базе. `verify` сверяет результат без изменения данных. Файлы, scope, seed, исходники, OID базы и хеш плана закреплены; несовпадение останавливает продолжение. Существующие несовпадающие выходные файлы не перезаписываются.

Эта процедура не содержит отдельной offline-команды назначения production-администратора или обновления занятой базы. После первой активации отдельный renderer подготавливает Compose и HTTPS-конфигурацию, как описано ниже. Первый владелец входит через проверенную Telegram-идентичность. Успешная подготовка не подтверждает рабочие покупки и не разрешает открывать трафик.

## Первый вход владельца

Административная роль определяется сервером по проверенному `Account.telegram_id`. Настройте `SUNRISE_OWNER_ID` своим точным Telegram ID строкой: совпадение даёт права владельца. `SUNRISE_ADMIN_TELEGRAM_ALLOWLIST` содержит JSON-список разрешённых ID строками и даёт административные права; набор возможностей администратора уже набора владельца. Ввод ID в форму, username или email сам по себе права не выдаёт.

Для браузерного Telegram-входа настройте у собственного бота в BotFather клиент Telegram Login, его секрет и разрешённый redirect URI. На сервере укажите `SUNRISE_TELEGRAM_LOGIN_CLIENT_ID`, `SUNRISE_TELEGRAM_LOGIN_CLIENT_SECRET` и `SUNRISE_TELEGRAM_LOGIN_REDIRECT_URI`. Последний должен точно совпадать с зарегистрированным HTTPS-адресом callback кабинета `/api/v1/auth/telegram/callback`. Секрет остаётся только на сервере. Подготовьте действующие юридические документы до входа.

Откройте кабинет, подтвердите документы и выполните настоящий Telegram OIDC-вход своей учётной записью. Backend проверяет ответ Telegram и связывает сессию с полученной идентичностью. Для первого входа владельца отдельная запись роли через offline-bootstrap не требуется: `StaffPermissions.role_in` учитывает совпадение проверенного Telegram ID с `SUNRISE_OWNER_ID`.

Далее настройте MFA в интерфейсе дополнительной защиты и подтвердите код приложения-аутентификатора. Начало настройки требует свежего подтверждённого входа; Telegram-сессия должна быть не старше пяти минут, в той же сессии и с того же IP. Защищённые действия сохраняют проверки возможностей и свежести MFA. `bootstrap_local_owner.py` остаётся инструментом только тестового контура.

Источник правил: [staff_permissions.py](../packages/infrastructure/src/sunrise_infrastructure/staff_permissions.py), [accounts.py](../packages/infrastructure/src/sunrise_infrastructure/accounts.py), [admin_mfa.py](../packages/infrastructure/src/sunrise_infrastructure/admin_mfa.py). Настоящий OIDC-вход и MFA в новой production-установке в рамках этой подготовки не проверялись; результат: NOT RUN.

## Первая активация

Отдельные команды `activation-plan`, `activate`, `verify-activation` предназначены только для первого перехода свежего экземпляра из закрытого состояния. База должна оставаться без аккаунтов, заказов, платежей и другой пользовательской активности. Существующий действующий экземпляр этим способом не переключается. Новое recurring enrollment остаётся выключенным.

Владелец заранее проверяет инфраструктуру и выбранные интеграции, затем предоставляет приватный JSON доказательств. Установщик сверяет файлы и хеши; сам внешние проверки не выполняет и провайдеров не вызывает. Запись `owner_review_confirmed=true` должна отражать реальный просмотр владельцем, а не использоваться для обхода проверки.

Формат задаёт `ActivationEvidence` в исходнике оператора: `version=1`, точный `scope_id`, `install_plan_sha256`, хеш исходного закрытого admission в `admission_sha256`, выбранный список `capabilities`, `reviewed_at` в UTC и `owner_review_confirmed=true`. Каждый элемент `checks` содержит `check_id`, `kind=owner_reviewed_external_artifact`, путь под `.private/production/evidence/`, SHA-256 файла и содержательное `summary`. Не включайте в доказательства секреты и клиентские данные.

| Выбранные возможности | Обязательные `check_id` |
| --- | --- |
| Любая первая активация | `tls_ingress` |
| `auth_mail`, `notice_email` | `smtp_delivery` |
| `invoice_create`, `payment_reconcile`, `financial_apply` | `payment_merchant`, `payment_callback` |
| `panel_mutation` | `panel_ownership` |
| `notice_telegram` | `telegram_identity` |
| `existing_recurring_status`, `existing_recurring_cancel` | `existing_recurring_only` |

Набор проверок должен точно соответствовать выбранным возможностям. Первая активация запрещает `broadcast_delivery`, `push_delivery` и `retention`. Остальные возможности выбираются явно, отдельным `--capability` для каждой; команды ниже показывают только пример `auth_mail`.

Для `invoice_create` одновременно обязательны явно выбранные `payment_reconcile`, `financial_apply` и `panel_mutation`. Неполный набор отклоняется; зависимости автоматически не включаются. Это минимальный набор для разрешения пути покупки. `auth_mail` выбирается отдельно, если нужен вход по почте.

В примере замените `SHA256_FROM_PLAN`, `YOUR_SCOPE_ID` и путь доказательств на свои проверенные значения. После просмотра плана используйте возвращённый `activation_plan_sha256` вместо `SHA256_FROM_ACTIVATION_PLAN`:

```powershell
.\.venv\Scripts\python.exe scripts/install_production_instance.py activation-plan --expected-plan-sha256 SHA256_FROM_PLAN --scope-id YOUR_SCOPE_ID --capability auth_mail --evidence-file .private/production/activation-evidence.json
.\.venv\Scripts\python.exe scripts/install_production_instance.py activate --expected-plan-sha256 SHA256_FROM_PLAN --expected-activation-sha256 SHA256_FROM_ACTIVATION_PLAN --scope-id YOUR_SCOPE_ID --confirm-first-activation
.\.venv\Scripts\python.exe scripts/install_production_instance.py verify-activation --expected-plan-sha256 SHA256_FROM_PLAN --expected-activation-sha256 SHA256_FROM_ACTIVATION_PLAN
```

Linux/macOS используют `.venv/bin/python` с теми же аргументами. `activation-plan` проверяет закрытое состояние и сохраняет отдельный план. `activate` создаёт новые `.private/production/first-activation-admission.json` и `first-activation-runtime.env`, затем согласованно обновляет marker/gates и сохраняет receipt с аудитом. Исходные закрытые артефакты сохраняются; несовпадающие файлы не перезаписываются. `verify-activation` проверяет файлы и состояние базы без записи.

Оператор не запускает сервисы, не меняет reverse proxy и не переключает env у существующего процесса. Применение нового runtime-файла к собственному deployment требует отдельно проверенного операторского шага. На настоящей изолированной PostgreSQL проверены `plan`, `init`, `admit`, `verify`, первая активация только `auth_mail`, принудительный откат транзакции и конкурентные блокировки. Результат не доказывает доставку письма, сетевую доступность реального callback или оплату; соответствующие доказательства предоставляет владелец.

## HTTPS runtime bundle

[render_production_runtime.py](../scripts/render_production_runtime.py) готовит отдельный неизменяемый набор файлов для собственного deployment после первой активации. Renderer читает локальные планы, исходники и сертификаты. Он не подключается к БД или провайдерам, не запускает Docker и не меняет работающие сервисы.

Заранее подготовьте два разных HTTPS-host в конфигурации экземпляра, DNS, firewall и сертификат с ключом. Сертификат должен подходить обоим именам, соответствовать ключу и быть действующим по времени. Renderer проверяет это локально; доверие публичного центра сертификации и доступность HTTPS по сети не проверяются. Автоматического ACME/Certbot и продления сертификатов этот набор не предоставляет.

Оба точных имени должны уже присутствовать в `SUNRISE_ALLOWED_HOSTS`. Renderer проверяет список и не добавляет имена автоматически; остальные разрешённые имена сохраняются.

PostgreSQL и Redis должны уже существовать вне создаваемого Compose, принадлежать этому экземпляру и быть доступны из контейнерной bridge-сети. Loopback-host в их DSN отклоняется. Настройте сетевой доступ и права своей инфраструктуры до проверки operator-профиля. Генератор не создаёт эти сервисы и не доказывает их доступность.

На Linux renderer запускается от root или UID `10001`. Новые копии защищённых файлов получают владельца `10001` и режим `0600`; исходные секреты и TLS-ключи сохраняются. Production-права Windows ACL не проверены. Команды ниже предназначены для своего Linux deployment; замените хеши и пути на фактические значения. CIDR является примером: выберите свободную приватную IPv4-подсеть `/24`..`/28`, не пересекающуюся с сетями установки.

```bash
.venv/bin/python scripts/render_production_runtime.py --expected-plan-sha256 SHA256_FROM_PLAN --expected-activation-sha256 SHA256_FROM_ACTIVATION_PLAN --tls-cert /etc/letsencrypt/live/YOUR_CERT/fullchain.pem --tls-key /etc/letsencrypt/live/YOUR_CERT/privkey.pem --ingress-subnet 172.30.65.0/28
```

Если нужны права root, используйте свой согласованный способ запуска с тем же явным Python. Вывод содержит путь `.private/production/runtime-bundle` и SHA-256 receipt. Набор включает `compose.json`, `nginx.conf`, `public-instance.json`, `receipt.json`, закрытые копии env/admission/instance/доказательств, TLS и разрешённых необязательных изображений. Повторная генерация не перезаписывает отличающиеся файлы. Не публикуйте bundle, раскрытый Compose и receipt с реквизитами установки.

Сначала соберите образы и выполните проверку допуска из фактической контейнерной сети:

```bash
docker compose -f .private/production/runtime-bundle/compose.json build
docker compose -f .private/production/runtime-bundle/compose.json --profile operator run --rm operator
```

Operator запускает существующий `install_production_instance.py verify-activation` с закреплёнными планами и доказательствами, проверяя БД без записи. Не переходите к запуску сервисов при неуспехе. После успешной проверки и разрешения владельца на запуск конкретного экземпляра:

```bash
docker compose -f .private/production/runtime-bundle/compose.json up -d
docker compose -f .private/production/runtime-bundle/compose.json ps -a
```

По умолчанию стартуют API, worker, одноразовый `web-init` и Nginx. `web-init` подставляет публичные метаданные в собранный статический frontend без сетевого fetch; Nginx читает итоговый том без права записи. Публичного Vite-сервера в этом наборе нет. Только Nginx публикует порты `80` и `443`; API и хранилища наружу не публикуются.

Бот включается отдельно только для собственного проверенного токена и разрешённого запуска polling:

```bash
docker compose -f .private/production/runtime-bundle/compose.json --profile bot up -d
```

API доверяет forwarded-заголовкам только от закреплённого IP Nginx в ingress-сети. Nginx заменяет клиентский `X-Forwarded-For`, задаёт HTTPS-схему и очищает `Forwarded`. На лендинге доступны только ограниченные публичные API-маршруты; Cookie/Authorization/CSRF и Set-Cookie отсекаются на этом proxy-пути. Административный HTML кабинета проверяет backend-доступ и возвращает 404 для неавторизованного запроса.

Сеть egress является обычной bridge-сетью с внешним доступом. Приложение продолжает проверять точные runtime scopes; это не firewall allowlist исходящих соединений. Настройка host firewall и доступности провайдеров остаётся задачей оператора.

Проверены чистые Docker-сборки backend/web/static, PostgreSQL и Redis, worker и бот с подменой транспорта. Выполнены 20 сценариев настоящего Nginx/HTTPS с собственным тестовым CA и 23 проверки runtime/Compose, включая ограничение webhook origin. Повторный прогон PostgreSQL-оператора выполнен с текущими хешами исходников.

В проверке `host-ingress-and-egress.json` страницы на Windows localhost-портах
`3100`/`3200`, каталог и публичная конфигурация вернули 200; закрытые API и
неавторизованный административный маршрут вернули 404. Контрольный внешний
endpoint был доступен из ingress, но прямое обращение к нему из приложения
завершилось отказом сети. Проверены loopback-привязки web-портов и отсутствие
опубликованных портов PostgreSQL.

Полный вход по почте, настоящий OIDC/MFA, публичное доверие TLS, live callback
и доставка внешних провайдеров не проверялись: NOT RUN. Не считайте изолированные
проверки подтверждением работающей production-интеграции.

## Приёмка установки

Если вы изменили программу и предоставляете пользователям взаимодействие с ней
по сети, [раздел 13 GNU AGPLv3](https://www.gnu.org/licenses/agpl-3.0.html#section13)
требует заметно предложить этим пользователям бесплатный доступ к соответствующим
исходникам вашей версии. Перед запуском разместите доступную пользователю ссылку
на исходники фактически развёрнутой версии, включая ваши изменения и необходимые
материалы сборки. Не включайте в них секреты и данные клиентов. Это обязанность
оператора; автоматическая публикация исходников приложением здесь не заявляется.
Полный текст лицензии также находится в [LICENSE](../LICENSE).

Перед открытием покупок проверьте восстановление резервной копии, вход и привязку аккаунтов, права администратора и MFA, актуальные юридические документы, общую цену сайта и бота, проверку webhook и обработку повторных событий. Отдельно проверьте, что отключённые возможности и пустые тестовые allowlist дают отказ.

Рабочий процесс обновления и отката описан в [UPDATING.md](UPDATING.md). Реальные списания, изменение банковских соглашений и рассылки требуют отдельного разрешения владельца своей установки.
