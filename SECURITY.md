# Безопасность / Security

## Сообщить об уязвимости

Сообщайте об уязвимостях приватно автору shipap: [work@bububi.icu](mailto:work@bububi.icu). Не публикуйте секреты, персональные данные или рабочие ссылки в открытом issue и не отправляйте токены, дампы и данные клиентов по почте.

Включён [GitHub Private Vulnerability Reporting](https://github.com/shipap/vless-vpn-shop-telegram-bot/security/advisories/new): можно отправить приватный отчёт через вкладку Security. Email выше также доступен. Сроки ответа и SLA не установлены. Контакт подключения RollyPay из README предназначен для заявок магазинов; сообщения об уязвимостях направляйте автору.

Укажите затронутую версию или ревизию, условия воспроизведения, наблюдаемое последствие и минимальный синтетический пример. Проверяйте проблему только на своей установке или с явным разрешением её владельца. Не проводите реальные списания, рассылки и изменения чужих VPN-аккаунтов ради подтверждения ошибки.

## Эксплуатация

Каждый оператор отвечает за секреты, HTTPS, резервные копии и восстановление своей установки. Ограничивайте доступ к PostgreSQL и Redis внутренней сетью. Разделяйте production и тестовые интеграции. Сохраняйте проверки подписи webhook, MFA, CSRF, владельца ресурса и ограничения получателей.

Первые публичные поддерживаемые версии и политика исправлений ещё не объявлены. Подготовка документации не означает завершённый аудит безопасности или подтверждённую установку с чистого окружения.

## Report a vulnerability

Report vulnerabilities privately to the author, shipap: [work@bububi.icu](mailto:work@bububi.icu). Keep secrets, customer data and usable links out of public issues. Do not email tokens, database dumps or customer records.

GitHub Private Vulnerability Reporting is [enabled](https://github.com/shipap/vless-vpn-shop-telegram-bot/security/advisories/new); submit a private report through the Security tab or use the email above. No response deadline or SLA has been established. The RollyPay onboarding contact in the README handles merchant applications; send vulnerability reports to the author.

Include the affected revision, prerequisites, impact and a minimal synthetic example. Test only systems you own or are explicitly authorized to assess.

Operators are responsible for their credentials, HTTPS, backup and restore procedures. Preserve webhook signature checks, MFA, CSRF, resource ownership checks and recipient restrictions. Supported public versions and a security update policy have not been announced.
