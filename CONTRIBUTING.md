# Участие в разработке

[Русский README](README.md) · [English README](README.en.md)

Перед изменениями прочитайте [архитектуру](docs/ARCHITECTURE.md) и [инструкцию установки](docs/INSTALL_RU.md). Для обсуждения ошибки подготовьте шаги воспроизведения, ожидаемый результат и обезличенный пример. Сообщения об уязвимостях направляйте по правилам [SECURITY.md](SECURITY.md).

## Изменения

Сайт и бот должны использовать одни правила аккаунтов, цен и платежей. Добавляйте бизнес-логику в общие сервисы, а обработчики интерфейсов оставляйте адаптерами. Миграции, финансовые расчёты и привязку аккаунтов проверяйте отдельно, включая повтор запроса и отказ внешнего провайдера.

Используйте синтетические данные и подмены провайдеров. Реальные списания, рассылки и изменения пользователей панели требуют отдельного разрешения владельца установки. Тестовые allowlist не должны разрешать отправку всем при пустом значении.

Брендинг VLESS VPN Shop задавайте через конфигурацию экземпляра. Сохраняйте [совместимость технических имён](docs/CONFIGURATION.md#совместимость-технических-имён). Пользовательский интерфейс и пояснения к настройке пишите по-русски, идентификаторы кода по-английски.

## Проверка

Для frontend в `package.json` определены `npm run typecheck`, `npm test` и `npm run build`. Python-тесты находятся в `tests`, настройки pytest и Ruff в `pyproject.toml`. Точный порядок подготовки среды и запуска смотрите в [установке](docs/INSTALL_RU.md). Здесь перечислены существующие команды, а не результаты их выполнения.

К изменению приложите назначение, затронутые сценарии и фактические результаты проверок. Для UI приложите обезличенные снимки desktop и mobile. Для исправления ошибки добавьте регрессионный тест, если он проверяет поведение и помогает предотвратить повторение.

Не включайте `.env`, дампы, логи, персональные данные, рабочие ссылки авторизации, платежей и подписок. Сохраняйте лицензии и уведомления сторонних компонентов. Собственный код распространяется на условиях [AGPL-3.0-only](LICENSE); сторонние условия указаны в [уведомлениях](docs/THIRD_PARTY_NOTICES.md).

## English

Read the [architecture](docs/ARCHITECTURE.md) and [installation guide](docs/INSTALL_EN.md). Keep account, pricing and payment rules shared between the website and bot. Use synthetic fixtures and mocked providers. Include reproduction steps and actual verification results with each change. Never include secrets, customer data or usable login, payment or subscription links. Report vulnerabilities using [SECURITY.md](SECURITY.md). Preserve third-party notices and the project license.
