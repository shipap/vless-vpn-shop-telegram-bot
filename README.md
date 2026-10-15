# VLESS VPN Shop

[Русский](README.md) · [English](README.en.md)

Самостоятельный VPN-магазин для VLESS: Telegram-бот, лендинг и личный кабинет для продажи подписок через Remnawave. Сайт и бот используют общую базу аккаунтов, тарифов, заказов и платежей.

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/UI-React%20%2B%20TypeScript-149ECA?logo=react&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/DB-PostgreSQL-4169E1?logo=postgresql&logoColor=white)

[Установка](docs/INSTALL_RU.md) · [Возможности](docs/FEATURES.md) · [Архитектура](docs/ARCHITECTURE.md) · [Безопасность](SECURITY.md)

## Что умеет

- Вход по коду из почты, через Telegram и Telegram Mini App; подтверждённая привязка аккаунтов.
- Покупка и продление подписок, переход между тарифами, изменение числа устройств с расчётом на сервере.
- Подключение к Remnawave, инструкции для клиента, список устройств и отзыв доступа.
- Промокоды, реферальные начисления, история покупок и уведомления.
- Админка для каталога, контента, пользователей, рассылок и финансовой аналитики.

Платёжная интеграция использует RollyPay. Доступные методы определяет настройка экземпляра. Новое подключение автопродления в live-режиме заблокировано кодом; обработка существующих соглашений имеет отдельные проверки. Подробности и ограничения: [возможности](docs/FEATURES.md).

## Интерфейс

Снимки настоящего интерфейса с синтетическими данными; цены и названия служат примером.

![Нейтральный лендинг](docs/assets/screenshots/landing-desktop.png)

<details>
<summary>Личный кабинет и управление сервисом</summary>

![Подписки](docs/assets/screenshots/customer-subscriptions.png)
![Обзор администратора](docs/assets/screenshots/admin-dashboard.png)
![Управление тарифами](docs/assets/screenshots/admin-catalog.png)

</details>

## Установка

Следуйте [инструкции установки](docs/INSTALL_RU.md). Для разработки предусмотрены синтетические данные, подмены провайдеров и ограничения получателей. Реальная установка требует собственных Telegram-, почтовых, Remnawave- и платёжных настроек.

Для новой production-установки есть поэтапная подготовка базы, отдельная первая активация и генератор HTTPS Compose-bundle со статическим frontend. Нужны собственные внешние PostgreSQL/Redis и TLS. Изолированная установка проверена с настоящими PostgreSQL/Redis и подменами внешних провайдеров; команды и границы проверок приведены в [руководстве развёртывания](docs/DEPLOYMENT.md).

> [!NOTE]
> Эта редакция готовится к первому публичному выпуску. Проверены чистые сборки Docker, миграции, доступ через локальный ingress, сетевые ограничения и изолированные сценарии с подменами провайдеров. Работа реальных платежей и внешних интеграций этими проверками не подтверждается.

## Как устроено

```mermaid
flowchart LR
    Landing[Лендинг React] --> API[FastAPI]
    Cabinet[Личный кабинет и админка React] --> API
    Telegram[Telegram] --> Bot[aiogram]
    API --> Shared[Общие сервисы Python]
    Bot --> Shared
    Worker[Фоновый worker] --> Shared
    Shared --> PG[(PostgreSQL)]
    Shared --> Redis[(Redis)]
    Shared --> Panel[Remnawave]
    Shared --> Pay[RollyPay]
    Shared --> Delivery[Telegram / почта / Web Push]
    Pay --> Hooks[HTTP webhooks]
    Panel --> Hooks
    Hooks --> API
```

Правила расчёта находятся в `packages/core`, сервисы и интеграции в `packages/infrastructure`. API, бот и worker подключают их из одного дерева исходников. Подробнее: [архитектура](docs/ARCHITECTURE.md).

## Свой экземпляр

Название, адреса, контакты и изображения задаются через типизированную конфигурацию: [instance.example.json](config/instance.example.json). Нейтральный профиль работает без логотипа. `APP_NAME` переопределяет название; `INSTANCE_CONFIG_PATH` выбирает файл экземпляра. Каталог и цены редактируются через общую базу и админку.

Автор: **shipap**. Собственный код: [AGPL-3.0-only](LICENSE). Условия сторонних компонентов сохраняются отдельно в [уведомлениях](docs/THIRD_PARTY_NOTICES.md).

[Конфигурация](docs/CONFIGURATION.md) · [Развёртывание](docs/DEPLOYMENT.md) · [Обновление](docs/UPDATING.md) · [Резервные копии](docs/BACKUP_RESTORE.md) · [Решение проблем](docs/TROUBLESHOOTING.md)

[Участие в разработке](CONTRIBUTING.md) · [Подготовка публикации](docs/GITHUB_PUBLICATION.md)

## Связь с автором

Автор VLESS VPN Shop: shipap. По вопросам разработки, доработок, интеграций, коммерческих проектов и сотрудничества:

[work@bububi.icu](mailto:work@bububi.icu) · [Telegram: @o3amfeels](https://t.me/o3amfeels) · [GitHub: shipap](https://github.com/shipap)

##  Подключение RollyPay

[![Подключить RollyPay](https://img.shields.io/badge/Подключить%20RollyPay-F0A45D?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/Victoria_rollypay?text=Здравствуйте%2C%20хочу%20подключить%20вашу%20платежку%2C%20рекомендация%20%40o3amfeels)

Кнопка открывает внешний контакт для подключения магазина к RollyPay с рекомендацией @o3amfeels. Решение о подключении принимает платёжный провайдер; одобрение не гарантируется. Рекомендация не означает официального одобрения проекта со стороны RollyPay. Об уязвимостях сообщайте через [канал безопасности](SECURITY.md).
