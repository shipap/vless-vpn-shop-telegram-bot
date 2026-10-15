# Подготовка публикации GitHub

Владелец разрешил публикацию публичной редакции в [shipap/vless-vpn-shop-telegram-bot](https://github.com/shipap/vless-vpn-shop-telegram-bot). Удалённый CI отдельно не настраивается. Результаты чистой локальной установки и оставшиеся ограничения приведены в [RELEASE_STATUS.md](RELEASE_STATUS.md).

## Сборка публичного ZIP

Используйте Python 3.12 и отдельный проверенный Gitleaks. Зависимости приложения
для упаковки не нужны. Проверена версия Gitleaks 8.30.1: скачайте подходящий архив
из [официального выпуска](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1)
и сверяйте его SHA-256 с опубликованным файлом контрольных сумм.

Из корня проекта, Windows:

```powershell
py -3.12 scripts/package_public_release.py --output .\vless-vpn-shop-telegram-bot-public-verified.zip --gitleaks C:\Tools\gitleaks.exe
```

Linux, с установленным Gitleaks:

```bash
python3.12 scripts/package_public_release.py --output ./vless-vpn-shop-telegram-bot-public-verified.zip --gitleaks /usr/local/bin/gitleaks
```

Скрипт выбирает исходники, документацию, тесты, миграции, lock-файлы и публичные
ресурсы по явному списку. `.artifacts`, `.env.docker`, локальные зависимости,
ключи и дампы не входят в архив. SQL допускается только для миграций и fixtures.
Не упаковывайте рабочую папку целиком через проводник или `Compress-Archive`.

Перед сохранением результата проверяются пути, CRC и контрольные суммы файлов.
Gitleaks сканирует распакованное содержимое и ASCII-строки бинарных ресурсов.
Ошибка сканера или найденный секрет блокируют создание итогового ZIP. Существующий
архив не перезаписывается: для следующей сборки выберите новое имя.

При добавлении форматов исходников или руководств обновляйте список упаковщика
и его тесты. Обязательные файлы проверяются на наличие. Изменение закреплённой
конфигурации Gitleaks требует отдельного ревью и обновления контрольной суммы.

Проверка упаковщика без зависимостей приложения:

```bash
python -B -m unittest tests.unit.test_public_package
```

Это проверка состава дистрибутива и известных шаблонов секретов. Она не заменяет
сканирование истории Git, аудит уязвимостей зависимостей, проверку прав на код
или испытания реальных интеграций. Текущие ограничения: [RELEASE_STATUS.md](RELEASE_STATUS.md).

## Метаданные

| Поле                       | Значение                                                                                                   |
| -------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Владелец / автор           | `shipap`                                                                                                   |
| Имя репозитория            | `vless-vpn-shop-telegram-bot`                                                                              |
| Description                | `Open-source VLESS VPN shop with Telegram bot, web dashboard, admin panel, Remnawave integration, payments and subscription management.` |
| Основной README            | Русский `README.md`                                                                                        |
| Английский README          | `README.en.md`                                                                                             |
| Лицензия собственного кода | `AGPL-3.0-only`                                                                                            |
| Website                    | Оставить пустым до появления публичного демо без реальных данных                                           |

Предлагаемые topics:

```text
vless vpn vpn-shop telegram-bot remnawave fastapi postgresql docker-compose self-hosted subscription-management aiogram python react typescript telegram-mini-app rollypay
```

GitHub разрешает до 20 topics, каждый длиной до 50 символов, из строчных букв, цифр и дефисов. Имена topics всегда публичны, включая topics приватных репозиториев. Эти правила проверены 15 октября 2026 года по [официальной документации GitHub](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics).

## Перед публикацией

- Проверить состав файлов и исключения Git/Docker: секреты, история частной установки, клиентские данные, дампы, логи и приватные изображения должны отсутствовать.
- Выполнить сканирование секретов и проверить найденные совпадения; сохранить обезличенный результат.
- Завершить проверку лицензий и происхождения зависимостей, изображений, шрифтов и заимствованных компонентов. Сохранить обязательные уведомления.
- Выполнить установку по обеим инструкциям на чистом окружении, миграции на синтетической базе, сборку frontend и применимые тесты.
- Проверить нейтральное оформление, ссылки документации и снимки интерфейса. Снимки должны содержать только синтетические данные.
- Выбрать поддерживаемые версии, проверить указанный в `SECURITY.md` приватный email и после публикации включить GitHub Private Vulnerability Reporting. Не заявлять доступность GitHub-канала до его включения.
- Получить отдельное разрешение владельца на конкретную публикацию после просмотра итогового состава.

Успешная проверка одного пункта не подтверждает остальные. Фактические результаты проверки установки и кода должны сопровождать выпуск; эта страница не является подтверждением их выполнения.

## Предлагаемый первый выпуск

Чистая локальная Docker-установка и PostgreSQL-проверки пройдены в указанном
в отчёте объёме. Для первого просмотра предлагается версия-кандидат `0.1.0-rc.1`.
Текущие manifests содержат `0.1.0`; изменение
версий и создание тега выполняются отдельно после решения о публикации.

Заголовок: `VLESS VPN Shop v0.1.0-rc.1 | First Public Release Candidate`.

Описание выпуска:

```text
Initial release candidate of a configurable VPN storefront with a Telegram bot,
website and admin dashboard. One Python backend and PostgreSQL database manage
accounts, catalog, orders, payments and Remnawave provisioning.

Includes email/Telegram authentication, confirmed account linking, subscription
renewals and tier changes, device management, promotions, referrals, notification
delivery and recovery of uncertain payment operations through provider reads.

New live recurring enrollment remains disabled. Operators must provide their own
provider credentials, legal documents, domains and protected runtime configuration.
See the installation guides and RELEASE_STATUS.md for verified checks
and remaining limitations. Test fixtures and screenshots contain synthetic data.
```

Это текст для последующего ручного просмотра. До завершения проверок не заменяйте
его утверждением о готовой production-установке. Если останутся блокирующие
проблемы, отложите выпуск, включая prerelease.

## About и настройки репозитория

- Description и topics: из таблицы выше; Website пока пустой.
- После разрешения владельца проверьте распознавание корневого `LICENSE` как AGPL.
- Включите приватные сообщения об уязвимостях, затем уточните `SECURITY.md`.
- Защитите основную ветку от прямой записи; требуйте review для изменений денег,
  идентичности, миграций и безопасности.
- Ограничьте Actions permissions чтением по умолчанию. CI и удалённые проверки
  настраивайте только после отдельного разрешения; не переносите production secrets.
- Перед включением автоматических обновлений зависимостей проверьте совместимость
  lock-файлов и лицензий; не включайте автоматическое слияние финансовых изменений.
- Создавайте release из проверенного тега и явно отмечайте prerelease. Контейнеры,
  готовые архивы и бинарные зависимости требуют отдельной проверки их состава.

Репозиторий создан публичным. Включены Issues, Discussions, Private Vulnerability Reporting, Dependabot alerts и security updates, secret scanning и push protection. Остальные пункты выше остаются рекомендациями. [Первый предварительный выпуск](https://github.com/shipap/vless-vpn-shop-telegram-bot/releases/tag/v0.1.0-rc.1) содержит ограничения проверки.
