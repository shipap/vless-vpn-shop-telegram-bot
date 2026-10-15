# Возможности

[README](../README.md) · [Установка](INSTALL_RU.md) · [Архитектура](ARCHITECTURE.md)

Функции ниже присутствуют в исходниках. Доступность внешних интеграций зависит от конфигурации, разрешений провайдера и режима запуска. Изолированная установка проверена с настоящими PostgreSQL/Redis, локальным Nginx ingress и подменами провайдеров. Реальные внешние операции этими результатами не подтверждаются.

## Аккаунт и вход

Пользователь входит по одноразовому коду из почты, через Telegram OIDC или подписанные данные Telegram Mini App. Код имеет серверный интервал повторной отправки 120 секунд и ограничение в десять ошибочных попыток. В кабинете доступны активные сессии и их отзыв.

Привязка Telegram к почтовому аккаунту требует подтверждений. Объединение работает с внутренним ID аккаунта и сохраняет связанные подписки, заказы и финансовые записи. Telegram ID может отсутствовать. Наличие почтового адреса само по себе не даёт административных прав.

Источники: [accounts.py](../packages/infrastructure/src/sunrise_infrastructure/accounts.py), [telegram_auth.py](../packages/infrastructure/src/sunrise_infrastructure/telegram_auth.py), [account_features.py](../packages/infrastructure/src/sunrise_infrastructure/account_features.py).

## Каталог и расчёт

Сайт и бот получают тарифы из одной базы. Владелец редактирует названия, описания, сроки, цены, число устройств и привязки к Remnawave. Сервер создаёт предложение цены, затем заказ с ключом идемпотентности. Цена в браузере не является доверенным основанием для оплаты.

Расчёт учитывает период, допустимые скидки, бонусы и внутренний кредит. Повышение тарифа рассчитывается по оставшемуся сроку; понижение переводит остаток стоимости в срок по условиям нового тарифа. Изменение числа устройств проходит отдельную проверку и расчёт. Деньги представлены через Decimal/NUMERIC.

Источники: [pricing.py](../packages/core/src/sunrise_core/pricing.py), [commerce.py](../packages/infrastructure/src/sunrise_infrastructure/commerce.py), [device_pricing.py](../packages/infrastructure/src/sunrise_infrastructure/device_pricing.py), [catalog.py](../packages/infrastructure/src/sunrise_infrastructure/catalog.py).

## Платежи и автопродление

RollyPay создаёт счёт для сохранённого заказа. Backend проверяет подтверждение провайдера и обрабатывает повторные события идемпотентно. Возврат пользователя со страницы оплаты сам по себе не подтверждает оплату. Комиссии и расчётная валюта хранятся отдельно; неизвестная комиссия остаётся неизвестной.

После потери ответа о создании счёта восстановление использует только GET-запросы к провайдеру. Оно ищет однозначное соответствие сохранённым параметрам операции, затем передаёт результат в общий путь проверки. Отсутствие результата не превращает заказ в оплаченный или неуспешный и не запускает повторное создание счёта. Для известных платежей есть отдельная сверка статуса и комиссии.

Код запрещает новое подключение автопродления в live-режиме. В других режимах подключение требует явного подтверждения условий, разрешённой возможности провайдера и включённой настройки. Выключение новых подключений сохраняет отдельные пути работы с существующими соглашениями и их отмены. Репозиторий не содержит универсального разрешения на реальные списания.

Источники: [billing.py](../packages/infrastructure/src/sunrise_infrastructure/billing.py), [unknown_invoices.py](../packages/infrastructure/src/sunrise_infrastructure/unknown_invoices.py), [recurring.py](../packages/infrastructure/src/sunrise_infrastructure/recurring.py), [providers.py](../packages/infrastructure/src/sunrise_infrastructure/providers.py).

## Подписки и устройства

Фоновые задания создают и обновляют доступ в Remnawave. В кабинете и боте доступны сведения о подписке, инструкции подключения и устройства. Отзыв устройств и смена реквизитов требуют проверки владельца, подтверждения операции и защиты от повторного исполнения.

Состояние панели обновляется при входе в профиль или подписку и перед покупкой. Подписанные события панели обновляют связанные данные; периодический массовый опрос пользователей не служит основным механизмом синхронизации. Очистка просроченного доступа проверяет принадлежность подписки этому экземпляру и сохраняет аккаунт с финансовой историей.

Источники: [provisioning.py](../packages/infrastructure/src/sunrise_infrastructure/provisioning.py), [customer_operations.py](../packages/infrastructure/src/sunrise_infrastructure/customer_operations.py), [panel_events.py](../packages/infrastructure/src/sunrise_infrastructure/panel_events.py), [retention.py](../packages/infrastructure/src/sunrise_infrastructure/retention.py).

## Промокоды и рефералы

Промокоды могут давать скидку либо доступ по заданным условиям. Проверки ограничивают срок, число использований и допустимые аккаунты или подписки. Реферальная ссылка передаёт атрибуцию, а начисления связаны с финансовыми операциями. Статистика приглашений доступна пользователю и администратору.

Источники: [promos.py](../packages/infrastructure/src/sunrise_infrastructure/promos.py), [referrals.py](../packages/infrastructure/src/sunrise_infrastructure/referrals.py), [referral_attribution.py](../packages/infrastructure/src/sunrise_infrastructure/referral_attribution.py).

## Уведомления и события

Центр уведомлений хранит сообщения и отметки прочтения. Доставка поддерживает Telegram, почту и Web Push с учётом настроек и ограничений получателей. Есть сообщения о подключении, устройствах, состоянии подписки, оплате и приближении продления. Доставка выполняется фоновыми заданиями.

Новости о доступности серверов показаны отдельной лентой с постраничной загрузкой. Устаревшие новости скрываются через 24 часа; техническая и финансовая история сохраняется. События Remnawave поступают в HTTP webhook и проходят проверку подписи. Отображаемые названия серверов используют сопоставления каталога и данных панели.

Источники: [notifications.py](../packages/infrastructure/src/sunrise_infrastructure/notifications.py), [subscription_engagement.py](../packages/infrastructure/src/sunrise_infrastructure/subscription_engagement.py), [panel_news.py](../packages/infrastructure/src/sunrise_infrastructure/panel_news.py), [node-news.tsx](../apps/web/dashboard/node-news.tsx).

## Администрирование

Администратор может редактировать каталог и публичный контент, управлять промокодами, просматривать пользователей, сессии и ограничения входа. Для защищённых действий применяются права, MFA и журналирование; чувствительные операции требуют свежего подтверждения.

Рассылки поддерживают оценку аудитории, медиа, сохранённые снимки получателей, остановку и отчёт доставки. Режим запуска и ограничения получателей проверяются отдельно. В production отбор рассылки не требует актуального принятия оферты, при этом покупка и работа с аккаунтом сохраняют свои юридические проверки. В тестовых режимах согласия и allowlist продолжают действовать.

Финансовая аналитика содержит фильтры и CSV-экспорт. Есть рассмотрение возвратов, ручные выдачи с причиной, личные сообщения и интерфейс заданий резервного копирования. Наличие интерфейса резервного копирования требует настроенного оператора и проверенного восстановления на стороне установки.

Источники: [admin.py](../packages/infrastructure/src/sunrise_infrastructure/admin.py), [admin_operations.py](../packages/infrastructure/src/sunrise_infrastructure/admin_operations.py), [broadcasts.py](../packages/infrastructure/src/sunrise_infrastructure/broadcasts.py), [admin_mfa.py](../packages/infrastructure/src/sunrise_infrastructure/admin_mfa.py), [main.py](../apps/api/src/sunrise_api/main.py).
