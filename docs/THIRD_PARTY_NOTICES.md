# Сторонние компоненты и лицензии

Проверено 15 октября 2026 года для локальной редакции `vless-vpn-shop-telegram-bot`.
Автор проекта — **shipap**. Лицензия собственного кода, на который автор вправе
предоставить разрешение, — **AGPL-3.0-only**. Полный неизменённый текст находится
в [LICENSE](../LICENSE). Это не меняет лицензии сторонних материалов и не
подтверждает права на унаследованные исходники.

`LICENSE` получен непосредственно с [сайта GNU](https://www.gnu.org/licenses/agpl-3.0.txt):
34 523 байта, SHA-256 `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`.
Указание `only` относится к выбору версии лицензии проекта; официальный текст GNU
ради этого не редактировался. При распространении и предоставлении изменённого
приложения по сети нужно соблюдать применимые требования AGPL, включая раздел 13.

## Самостоятельные компоненты и историческое происхождение

`CodeSlots.jsx`, `CodeSlots.css`, `CodeSlots.d.ts`, а также `reactbits/BellToggle.tsx`,
`BellToggle.css`, `SwipeToast.tsx` и `SwipeToast.css` полностью заменены
самостоятельными реализациями shipap под AGPL-3.0-only. Реализация использует
нативные input/button/popover, события указателя и таймеры React/DOM, а также
существующие Lucide-иконки. Старые имена импортов, типы параметров и CSS-селекторы
сохранены как контракт приложения. Прежние анимационные алгоритмы, константы,
SVG-пути и стили в новые файлы не переносились. Устаревшие настройки прежних
эффектов принимаются для совместимости вызовов и не воспроизводят прежний дизайн.

| Самостоятельный компонент | Текущие вызовы |
| --- | --- |
| `packages/web-ui/CodeSlots` | Поля кода в `apps/web/dashboard/auth.tsx`, `account-features.tsx`, `admin-mfa.tsx`, `admin.tsx`, `customer.tsx` |
| `packages/web-ui/reactbits/BellToggle` | Колокольчик в `packages/web-ui/notification-center.tsx`; нативный popover, количество непрочитанных |
| `packages/web-ui/reactbits/SwipeToast` | Уведомления в `packages/web-ui/notification-center.tsx`; таймер с паузой, кнопки, Escape, свайп и отмена захвата указателя |

Ранее на этих путях находились адаптации React Bits: предоставленный владельцем
CodeSlots с неустановленной точной ревизией и BellToggle/SwipeToast с указанной
ревизией `b2098591`. Исторический **Copyright (c) 2026 David Haz** и полный
[MIT + Commons Clause](../packages/web-ui/reactbits/LICENSE.md) сохранены как
свидетельство прежнего происхождения. [Upstream этой ревизии](https://github.com/DavidHDev/react-bits/blob/b2098591/LICENSE.md)
содержит дополнительное ограничение распространения самих компонентов, включая
наборы, и не является обычной MIT-лицензией. Сохранённый исторический текст не
перелицензирован и не применяется вместо AGPL к самостоятельно написанным файлам.
Наличие этого документа не даёт разрешения вернуть прежний ограниченный код в
распространяемый исходник или сборку.

Проверка функциональности замен включает отдельный локальный браузерный стенд
`tests/e2e/original-controls.config.ts`. Независимый итоговый review координатора
проверяет полноту удаления прежних реализаций и совместимость приложения; этот
документ не выдаёт автоматическую проверку метаданных за юридическое заключение.

Итоговая проверка сопоставила текущие JS/TSX и CSS с шестью соответствующими
файлами upstream `b2098591`. Сохранились совместимые объявления параметров,
имена классов и короткие обычные фрагменты JSX/CSS. Прежних реализаций движения,
физических расчётов и встроенных SVG-путей в текущих файлах нет. Эта проверка
не устанавливает точную ревизию ранее переданного CodeSlots и не доказывает
независимую историю создания по процедуре clean-room. Если потребуется отдельное
подтверждение прав на сохранённые части, безопасные варианты: письменное разрешение
правообладателя либо реализация по функциональной спецификации с отдельно
проверенным происхождением, сохраняя поля кода и уведомления приложения.

Повторное независимое техническое ревью 15 октября 2026 года разрешило полную
upstream-ревизию: `b2098591ad5b3489eff65ca9e5b9f9bdf2de29c3`. Сопоставлены шесть
файлов реализаций и стилей. В трёх CSS нет одинаковых блоков от четырёх строк;
совпадения JS/TSX ограничены сигнатурой функции, объявлениями параметров и обычными
конструкциями интерфейса. Ограниченная прежняя реализация в текущем составе не
обнаружена; оснований повторно переписывать эти компоненты не выявлено.
Оригинальная ревизия старого CodeSlots остаётся неизвестной, но старая реализация
не распространяется. Это техническая проверка текущих файлов, а не юридическое
подтверждение всей истории авторства.

Повторно скачаны точные npm-архивы Manrope 5.3.0, Hugeicons React 1.1.10 и
Core Free 4.3.5, Lucide 1.52.0, Motion 14.0.0, QRCode 1.5.4, React/React DOM
19.3.0. Их SHA-512 совпали с integrity в lock-файле; тексты лицензий присутствуют.
`PRO-LICENSE.md` внутри обёртки Hugeicons относится к `@hugeicons-pro/*`;
публичный проект использует пакеты с отдельными MIT-уведомлениями, Pro-наборов нет.

## Шрифты, иконки и медиа

| Материал | Где находится / используется | Условия и attribution |
| --- | --- | --- |
| Twemoji Country Flags, `country-flag-emoji-polyfill` 0.1.8 | `packages/web-ui/fonts/TwemojiCountryFlags.woff2`; подключение через `flag-font.css` | Copyright (c) 2022 TalkJS, код MIT; графика Twemoji — CC BY 4.0. [Сохранённое уведомление](../packages/web-ui/fonts/TwemojiCountryFlags.LICENSE.md), [upstream](https://github.com/talkjs/country-flag-emoji-polyfill/blob/master/LICENSE.md), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/legalcode). Бинарник побайтно совпал с [npm-архивом 0.1.8](https://registry.npmjs.org/country-flag-emoji-polyfill/-/country-flag-emoji-polyfill-0.1.8.tgz); SHA-256 `9f04f14429bb6a9f415c7a4dd902a918d7e81a4f7526c415496fdb063954e3b8`. Это subset upstream без локальных изменений |
| Twemoji / Mozilla Twemoji COLR | Источник графики и технологии шрифта флагов | Графика: Twitter и участники Twemoji, CC BY 4.0; [исходный проект](https://github.com/twitter/twemoji#license). Инструменты Mozilla: Copyright 2016–2018 Mozilla Foundation, Apache-2.0; [уведомление](https://github.com/mozilla/twemoji-colr/blob/master/LICENSE.md). Инструменты сборки Mozilla в эту редакцию не скопированы |
| Manrope через `@fontsource-variable/manrope` 5.3.0 | Импорт в `apps/web/landing/main.tsx` и `apps/web/dashboard/main.tsx`; пакет устанавливается отдельно | OFL-1.1. Copyright 2019 The Manrope Project Authors. [Текст и авторы](https://github.com/fontsource/font-files/blob/main/fonts/google/manrope/LICENSE). При включении в frontend-сборку нужны его собственные copyright и полный OFL |
| Lucide React 1.52.0 | Иконки landing, dashboard и общих компонентов | Закреплённый пакет объявляет ISC. [Upstream LICENSE](https://github.com/lucide-icons/lucide/blob/main/LICENSE) также сохраняет MIT и Copyright (c) 2013-present Cole Bemis для перечисленных иконок из Feather. Сохраняются оба уведомления; одно поле ISC в lockfile их не заменяет |
| Hugeicons React 1.1.10 и Core Free Icons 4.3.5 | В manifest остаются как закреплённые зависимости; новые три компонента их не импортируют | Оба закреплённых npm-пакета объявляют MIT отдельно. Условия обёртки не используются как доказательство условий набора иконок. Copyright (c) 2025 Hugeicons. Полные LICENSE обоих точных архивов приведены ниже |
| `tests/fixtures/broadcast-chromium-synthetic.mp4` | Одноцветные кадры 64×64 для тестов загрузки | Локальная фикстура без внешних изображений; [описание происхождения](../tests/fixtures/BROADCAST-MP4-PROVENANCE.md). Генератор в публичной редакции отсутствует; описанная там историческая проверка не выдаётся за новую |
| `docs/assets/screenshots/*.png` | Семь снимков нейтрального React-интерфейса для документации | Снимки локального стенда с синтетическими GET-ответами по [отчёту подготовки](RELEASE_STATUS.md). Права на сторонние шрифты и иконки сохраняются; снимки не объявляют их собственностью автора проекта |

Поиск включённых `.woff`, `.woff2`, `.ttf`, `.otf`, `.svg`, `.png`, `.webp`, `.jpg`,
`.jpeg`, `.gif`, `.ico` и видео обнаружил названные WOFF2, MP4 и семь PNG-снимков. Частные
logo/favicon/mascot, `instance-assets/`, `node_modules/`, виртуальное окружение
и готовые сборки в проверенный набор не входят. Упоминание отсутствующих
`examples/assets/` из прежнего уведомления удалено. Настраиваемые материалы
будущего установщика требуют его собственных прав.

## Дополнение от 16 октября 2026 года: логотипы платёжных сервисов

В `docs/assets/payment-providers/` добавлены неизменённые SVG-знаки RollyPay
и Platega с официальных сайтов для обозначения контактов подключения в README.
Их источники, размеры, SHA-256 и принадлежность прав записаны в
[PROVENANCE.md](assets/payment-providers/PROVENANCE.md). Отдельная свободная
лицензия на эти изображения не обнаружена; они не перелицензируются под AGPL
собственного кода. Размещение логотипов не означает одобрения проекта провайдерами.

## Пределы проверки зависимостей

Ниже перечислены все 146 записей `package-lock.json` и все 66 версий
`requirements.lock`. Для npm лицензии прочитаны из закреплённого lockfile; для
Python 15 октября 2026 запрошены метаданные **каждой точной версии** через официальный
`https://pypi.org/pypi/<name>/<version>/json`. Все 66 запросов успешны. При наличии
используется `license_expression`, затем поле `license`, затем классификатор.
Это проверка метаданных, а не утверждение, что просмотрен каждый файл каждого
архива. В таблице npm `dev` — флаг lockfile, а не доказательство попадания в bundle.

Отдельно от runtime lock-файла 15 октября проверены build-инструменты
setuptools 83.0.0 и wheel 0.46.2: точные метаданные PyPI объявляют MIT.
Bootstrap использует pip 26.2.1, также с MIT-лицензией и собственными
уведомлениями о vendored-зависимостях; его бинарник не входит в исходный ZIP.
Источники: [setuptools](https://pypi.org/pypi/setuptools/83.0.0/json),
[wheel](https://pypi.org/pypi/wheel/0.46.2/json). Их бинарные пакеты в исходный
ZIP не включены; при распространении готового окружения сохраняйте их тексты MIT.

Особого сохранения условий требуют `psycopg` / `psycopg-binary` (LGPL-3.0-only),
MPL-компоненты и составные лицензии. [Документация Psycopg](https://www.psycopg.org/psycopg3/docs/basic/install.html#binary-installation)
подтверждает наличие клиентских библиотек в binary-пакете. При распространении
wheel, окружения или Docker-образа нужно инвентаризировать конкретные бинарники,
их LICENSE, NOTICE, исходники и native-зависимости: метаданные Python не описывают
полностью libpq, OpenSSL, Pillow-кодеки и другие встроенные библиотеки.
`colorama` и Python `qrcode` объявляют BSD без точного варианта, `lupa` — MIT style;
их точные тексты нельзя заменять догадкой по названию.

`infra/Dockerfile.production` задаёт внешний `BASE_IMAGE` и устанавливает `ffmpeg`
из apt без закреплённого состава. Эти бинарники здесь не поставляются. Лицензии
образа, FFmpeg, кодеков, системных пакетов, PostgreSQL, Redis, Nginx и браузеров
проверяются для фактически распространяемого артефакта. Лицензия Python-клиента
`redis` не является лицензией сервера Redis. Этот исходный ZIP не распространяет
Docker-образы, wheel или установленные окружения; заключение об их полном
бинарном составе и совместимости здесь не даётся.

## Инструкционные навыки при подготовке документации

Проверенные копии навыков находятся в локальном `.agents/skills/`; Docker их исключает,
а Git разрешает только явно перечисленные копии и их provenance. Использование как инструкций
само по себе не означает включение их текста в продукт. При последующем копировании
текста или существенных частей нужно включить соответствующий полный MIT-текст
и copyright, сохранив следующие точные источники:

| Источник | Ревизия | Лицензия и copyright |
| --- | --- | --- |
| `github/awesome-copilot`, create-readme | `36e95dfda72140a68f577d5c894a551607606bd3` | MIT, Copyright GitHub, Inc.; [LICENSE этой ревизии](https://github.com/github/awesome-copilot/blob/36e95dfda72140a68f577d5c894a551607606bd3/LICENSE) |
| `blader/humanizer` | `225a6f39ac85f76ee48dbad772ea4abe4ed6c9d8` | MIT, Copyright (c) 2025 Siqi Chen; [LICENSE этой ревизии](https://github.com/blader/humanizer/blob/225a6f39ac85f76ee48dbad772ea4abe4ed6c9d8/LICENSE) |

## Права на унаследованный прикладной код

Выбор автора и лицензии проекта зафиксирован по решению владельца. Поиск заголовков
copyright в этой редакции обнаружил перечисленные сторонние уведомления, но не
установил отдельную цепочку прав на все ранее переданные прикладные исходники.
Отсутствие заголовка не доказывает отсутствие чужих прав. Нельзя приписывать shipap
авторство сторонних частей или считать все унаследованные права автоматически
переданными. В текущем исходном дистрибутиве техническое ревью не выявило
несовместимых сторонних реализаций: ограниченные React Bits заменены, их
уведомления сохранены исторически. Это не юридическое подтверждение всей цепочки
авторства. При появлении конкретного неучтённого заимствования потребуются
проверка его прав и, при необходимости, замена или разрешение правообладателя.

## Закреплённые npm-зависимости

| Запись lockfile | Версия | Лицензия в lockfile | Флаг dev |
| --- | --- | --- | --- |
| `@axe-core/playwright` | 4.13.0 | MPL-2.0 | да |
| `@babel/code-frame` | 7.29.7 | MIT | да |
| `@babel/helper-validator-identifier` | 7.29.7 | MIT | да |
| `@fontsource-variable/manrope` | 5.3.0 | OFL-1.1 | нет |
| `@hugeicons/core-free-icons` | 4.3.5 | MIT | нет |
| `@hugeicons/react` | 1.1.10 | MIT | нет |
| `@jridgewell/resolve-uri` | 3.1.2 | MIT | да |
| `@jridgewell/sourcemap-codec` | 1.6.0 | MIT | да |
| `@jridgewell/trace-mapping` | 0.3.31 | MIT | да |
| `@oxc-project/types` | 0.153.0 | MIT | да |
| `@playwright/test` | 1.63.0 | Apache-2.0 | да |
| `@redocly/ajv` | 8.11.2 | MIT | да |
| `@redocly/config` | 0.22.0 | MIT | да |
| `@redocly/openapi-core` | 1.34.20 | MIT | да |
| `@rolldown/binding-android-arm-eabi` | 1.2.13 | MIT | да |
| `@rolldown/binding-android-arm64` | 1.2.13 | MIT | да |
| `@rolldown/binding-darwin-arm64` | 1.2.13 | MIT | да |
| `@rolldown/binding-darwin-x64` | 1.2.13 | MIT | да |
| `@rolldown/binding-freebsd-x64` | 1.2.13 | MIT | да |
| `@rolldown/binding-linux-arm-gnueabihf` | 1.2.13 | MIT | да |
| `@rolldown/binding-linux-arm64-gnu` | 1.2.13 | MIT | да |
| `@rolldown/binding-linux-arm64-musl` | 1.2.13 | MIT | да |
| `@rolldown/binding-linux-ppc64-gnu` | 1.2.13 | MIT | да |
| `@rolldown/binding-linux-s390x-gnu` | 1.2.13 | MIT | да |
| `@rolldown/binding-linux-x64-gnu` | 1.2.13 | MIT | да |
| `@rolldown/binding-linux-x64-musl` | 1.2.13 | MIT | да |
| `@rolldown/binding-openharmony-arm64` | 1.2.13 | MIT | да |
| `@rolldown/binding-win32-arm64-msvc` | 1.2.13 | MIT | да |
| `@rolldown/binding-win32-x64-msvc` | 1.2.13 | MIT | да |
| `@rolldown/pluginutils` | 1.0.1 | MIT | да |
| `@types/chai` | 5.2.3 | MIT | да |
| `@types/deep-eql` | 4.0.2 | MIT | да |
| `@types/estree` | 1.0.9 | MIT | да |
| `@types/node` | 26.6.4 | MIT | да |
| `@types/qrcode` | 1.5.6 | MIT | да |
| `@types/react` | 19.3.0 | MIT | нет |
| `@types/react-dom` | 19.3.0 | MIT | да |
| `@vitejs/plugin-react` | 6.1.2 | MIT | да |
| `@vitest/mocker` | 5.0.3 | MIT | да |
| `@vitest/spy` | 5.0.3 | MIT | да |
| `agent-base` | 7.1.4 | MIT | да |
| `ansi-colors` | 4.1.3 | MIT | да |
| `ansi-regex` | 5.0.1 | MIT | нет |
| `ansi-styles` | 4.3.0 | MIT | нет |
| `argparse` | 2.0.1 | Python-2.0 | да |
| `assertion-error` | 2.0.1 | MIT | да |
| `axe-core` | 4.13.0 | MPL-2.0 | да |
| `balanced-match` | 1.0.2 | MIT | да |
| `brace-expansion` | 2.1.7 | MIT | да |
| `camelcase` | 5.3.1 | MIT | нет |
| `chai` | 6.3.0 | MIT | да |
| `change-case` | 5.4.4 | MIT | да |
| `cliui` | 6.0.0 | ISC | нет |
| `color-convert` | 2.0.1 | MIT | нет |
| `color-name` | 1.1.4 | MIT | нет |
| `colorette` | 1.4.0 | MIT | да |
| `csstype` | 3.2.3 | MIT | нет |
| `debug` | 4.4.3 | MIT | да |
| `decamelize` | 1.2.0 | MIT | нет |
| `detect-libc` | 2.1.2 | Apache-2.0 | да |
| `dijkstrajs` | 1.0.3 | MIT | нет |
| `emoji-regex` | 8.0.0 | MIT | нет |
| `es-module-lexer` | 2.3.2 | MIT | да |
| `estree-walker` | 3.0.3 | MIT | да |
| `expect-type` | 1.4.0 | Apache-2.0 | да |
| `fast-deep-equal` | 3.1.3 | MIT | да |
| `fdir` | 6.5.0 | MIT | да |
| `find-up` | 4.1.0 | MIT | нет |
| `framer-motion` | 14.0.0 | MIT | нет |
| `fsevents` | 2.3.3 | MIT | да |
| `get-caller-file` | 2.0.5 | ISC | нет |
| `https-proxy-agent` | 7.0.6 | MIT | да |
| `index-to-position` | 1.2.0 | MIT | да |
| `is-fullwidth-code-point` | 3.0.0 | MIT | нет |
| `js-levenshtein` | 1.1.6 | MIT | да |
| `js-tokens` | 4.0.0 | MIT | да |
| `js-yaml` | 4.3.2 | MIT | да |
| `json-schema-traverse` | 1.0.0 | MIT | да |
| `lightningcss` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-android-arm64` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-darwin-arm64` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-darwin-x64` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-freebsd-x64` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-linux-arm-gnueabihf` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-linux-arm64-gnu` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-linux-arm64-musl` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-linux-x64-gnu` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-linux-x64-musl` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-win32-arm64-msvc` | 1.33.0 | MPL-2.0 | да |
| `lightningcss-win32-x64-msvc` | 1.33.0 | MPL-2.0 | да |
| `locate-path` | 5.0.0 | MIT | нет |
| `lucide-react` | 1.52.0 | ISC | нет |
| `magic-string` | 1.4.3 | MIT | да |
| `minimatch` | 5.1.9 | ISC | да |
| `motion` | 14.0.0 | MIT | нет |
| `motion-dom` | 14.0.0 | MIT | нет |
| `motion-utils` | 14.0.0 | MIT | нет |
| `ms` | 2.1.3 | MIT | да |
| `nanoid` | 3.3.20 | MIT | да |
| `obug` | 2.2.1 | MIT | да |
| `openapi-typescript` | 7.13.0 | MIT | да |
| `openapi-typescript/node_modules/yargs-parser` | 21.1.1 | ISC | да |
| `p-limit` | 2.3.0 | MIT | нет |
| `p-locate` | 4.1.0 | MIT | нет |
| `p-try` | 2.2.0 | MIT | нет |
| `parse-json` | 8.3.0 | MIT | да |
| `path-exists` | 4.0.0 | MIT | нет |
| `picocolors` | 1.1.1 | ISC | да |
| `picomatch` | 4.0.7 | MIT | да |
| `playwright` | 1.63.0 | Apache-2.0 | да |
| `playwright-core` | 1.63.0 | Apache-2.0 | да |
| `pluralize` | 8.0.0 | MIT | да |
| `pngjs` | 5.0.0 | MIT | нет |
| `postcss` | 8.5.29 | MIT | да |
| `prettier` | 3.9.9 | MIT | да |
| `qrcode` | 1.5.4 | MIT | нет |
| `react` | 19.3.0 | MIT | нет |
| `react-dom` | 19.3.0 | MIT | нет |
| `require-directory` | 2.1.1 | MIT | нет |
| `require-from-string` | 2.0.2 | MIT | да |
| `require-main-filename` | 2.0.0 | ISC | нет |
| `rolldown` | 1.2.13 | MIT | да |
| `scheduler` | 0.28.0 | MIT | нет |
| `set-blocking` | 2.0.0 | ISC | нет |
| `source-map-js` | 1.2.2 | BSD-3-Clause | да |
| `std-env` | 4.3.0 | MIT | да |
| `string-width` | 4.2.3 | MIT | нет |
| `strip-ansi` | 6.0.1 | MIT | нет |
| `supports-color` | 10.2.2 | MIT | да |
| `tinybench` | 6.2.1 | MIT | да |
| `tinyexec` | 1.3.1 | MIT | да |
| `tinyglobby` | 0.2.17 | MIT | да |
| `tslib` | 2.8.1 | 0BSD | нет |
| `type-fest` | 4.41.0 | (MIT OR CC0-1.0) | да |
| `typescript` | 5.9.3 | Apache-2.0 | да |
| `undici-types` | 8.9.0 | MIT | да |
| `uri-js-replace` | 1.0.1 | MIT | да |
| `vite` | 8.3.3 | MIT | да |
| `vitest` | 5.0.3 | MIT | да |
| `which-module` | 2.0.1 | ISC | нет |
| `why-is-node-running` | 3.2.1 | MIT | да |
| `wrap-ansi` | 6.2.0 | MIT | нет |
| `y18n` | 4.0.3 | ISC | нет |
| `yaml-ast-parser` | 0.0.43 | Apache-2.0 | да |
| `yargs` | 15.4.1 | MIT | нет |
| `yargs-parser` | 18.1.3 | ISC | нет |

## Закреплённые Python-зависимости

| Пакет | Версия | Лицензия в метаданных | Первичный источник |
| --- | --- | --- | --- |
| `aiofiles` | 25.1.0 | Apache-2.0 | [PyPI](https://pypi.org/pypi/aiofiles/25.1.0/json) |
| `aiogram` | 3.31.0 | MIT | [PyPI](https://pypi.org/pypi/aiogram/3.31.0/json) |
| `aiohappyeyeballs` | 2.7.1 | PSF-2.0 | [PyPI](https://pypi.org/pypi/aiohappyeyeballs/2.7.1/json) |
| `aiohttp` | 3.14.4 | Apache-2.0 AND MIT | [PyPI](https://pypi.org/pypi/aiohttp/3.14.4/json) |
| `aiosignal` | 1.4.0 | Apache 2.0 | [PyPI](https://pypi.org/pypi/aiosignal/1.4.0/json) |
| `alembic` | 1.20.0 | MIT | [PyPI](https://pypi.org/pypi/alembic/1.20.0/json) |
| `annotated-doc` | 0.0.5 | MIT | [PyPI](https://pypi.org/pypi/annotated-doc/0.0.5/json) |
| `annotated-types` | 0.8.0 | MIT | [PyPI](https://pypi.org/pypi/annotated-types/0.8.0/json) |
| `anyio` | 4.15.1 | MIT | [PyPI](https://pypi.org/pypi/anyio/4.15.1/json) |
| `argon2-cffi` | 25.1.0 | MIT | [PyPI](https://pypi.org/pypi/argon2-cffi/25.1.0/json) |
| `argon2-cffi-bindings` | 26.1.0 | MIT | [PyPI](https://pypi.org/pypi/argon2-cffi-bindings/26.1.0/json) |
| `attrs` | 26.1.0 | MIT | [PyPI](https://pypi.org/pypi/attrs/26.1.0/json) |
| `certifi` | 2026.7.22 | MPL-2.0 | [PyPI](https://pypi.org/pypi/certifi/2026.7.22/json) |
| `cffi` | 2.1.1 | MIT-0 | [PyPI](https://pypi.org/pypi/cffi/2.1.1/json) |
| `charset-normalizer` | 3.5.2 | MIT | [PyPI](https://pypi.org/pypi/charset-normalizer/3.5.2/json) |
| `click` | 8.5.0 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/click/8.5.0/json) |
| `colorama` | 0.4.6 | License :: OSI Approved :: BSD License | [PyPI](https://pypi.org/pypi/colorama/0.4.6/json) |
| `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause | [PyPI](https://pypi.org/pypi/cryptography/50.0.2/json) |
| `dnspython` | 2.8.0 | ISC | [PyPI](https://pypi.org/pypi/dnspython/2.8.0/json) |
| `email-validator` | 2.3.0 | Unlicense | [PyPI](https://pypi.org/pypi/email-validator/2.3.0/json) |
| `fakeredis` | 2.39.0 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/fakeredis/2.39.0/json) |
| `fastapi` | 0.142.2 | MIT | [PyPI](https://pypi.org/pypi/fastapi/0.142.2/json) |
| `frozenlist` | 1.8.0 | Apache-2.0 | [PyPI](https://pypi.org/pypi/frozenlist/1.8.0/json) |
| `h11` | 0.16.0 | MIT | [PyPI](https://pypi.org/pypi/h11/0.16.0/json) |
| `http_ece` | 1.2.1 | MIT | [PyPI](https://pypi.org/pypi/http_ece/1.2.1/json) |
| `httpcore` | 1.0.9 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/httpcore/1.0.9/json) |
| `httpx` | 0.28.1 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/httpx/0.28.1/json) |
| `hypothesis` | 6.168.5 | MPL-2.0 | [PyPI](https://pypi.org/pypi/hypothesis/6.168.5/json) |
| `idna` | 3.20 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/idna/3.20/json) |
| `iniconfig` | 2.3.1 | MIT | [PyPI](https://pypi.org/pypi/iniconfig/2.3.1/json) |
| `lupa` | 2.8 | MIT style | [PyPI](https://pypi.org/pypi/lupa/2.8/json) |
| `magic-filter` | 1.0.12 | License :: OSI Approved :: MIT License | [PyPI](https://pypi.org/pypi/magic-filter/1.0.12/json) |
| `Mako` | 1.4.3 | MIT | [PyPI](https://pypi.org/pypi/Mako/1.4.3/json) |
| `MarkupSafe` | 3.0.4 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/MarkupSafe/3.0.4/json) |
| `multidict` | 6.9.1 | Apache License 2.0 | [PyPI](https://pypi.org/pypi/multidict/6.9.1/json) |
| `opentelemetry-api` | 1.45.1 | Apache-2.0 | [PyPI](https://pypi.org/pypi/opentelemetry-api/1.45.1/json) |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause | [PyPI](https://pypi.org/pypi/packaging/26.3/json) |
| `pillow` | 12.3.0 | MIT-CMU | [PyPI](https://pypi.org/pypi/pillow/12.3.0/json) |
| `pluggy` | 1.6.0 | MIT | [PyPI](https://pypi.org/pypi/pluggy/1.6.0/json) |
| `propcache` | 0.5.4 | Apache-2.0 | [PyPI](https://pypi.org/pypi/propcache/0.5.4/json) |
| `psycopg` | 3.3.6 | LGPL-3.0-only | [PyPI](https://pypi.org/pypi/psycopg/3.3.6/json) |
| `psycopg-binary` | 3.3.6 | LGPL-3.0-only | [PyPI](https://pypi.org/pypi/psycopg-binary/3.3.6/json) |
| `py-vapid` | 1.9.4 | MPL-2.0 | [PyPI](https://pypi.org/pypi/py-vapid/1.9.4/json) |
| `pycparser` | 3.0 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/pycparser/3.0/json) |
| `pydantic` | 2.13.5 | MIT | [PyPI](https://pypi.org/pypi/pydantic/2.13.5/json) |
| `pydantic_core` | 2.46.5 | MIT | [PyPI](https://pypi.org/pypi/pydantic_core/2.46.5/json) |
| `pydantic-settings` | 2.15.0 | MIT | [PyPI](https://pypi.org/pypi/pydantic-settings/2.15.0/json) |
| `Pygments` | 2.21.0 | BSD-2-Clause | [PyPI](https://pypi.org/pypi/Pygments/2.21.0/json) |
| `PyJWT` | 2.15.1 | MIT | [PyPI](https://pypi.org/pypi/PyJWT/2.15.1/json) |
| `PyOTP` | 2.10.0 | MIT | [PyPI](https://pypi.org/pypi/PyOTP/2.10.0/json) |
| `pytest` | 9.1.1 | MIT | [PyPI](https://pypi.org/pypi/pytest/9.1.1/json) |
| `python-dotenv` | 1.2.4 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/python-dotenv/1.2.4/json) |
| `pywebpush` | 2.5.0 | MPL-2.0 | [PyPI](https://pypi.org/pypi/pywebpush/2.5.0/json) |
| `qrcode` | 8.2 | BSD | [PyPI](https://pypi.org/pypi/qrcode/8.2/json) |
| `redis` | 6.4.0 | MIT | [PyPI](https://pypi.org/pypi/redis/6.4.0/json) |
| `requests` | 2.34.2 | Apache-2.0 | [PyPI](https://pypi.org/pypi/requests/2.34.2/json) |
| `ruff` | 0.16.10 | MIT | [PyPI](https://pypi.org/pypi/ruff/0.16.10/json) |
| `sortedcontainers` | 2.4.0 | Apache 2.0 | [PyPI](https://pypi.org/pypi/sortedcontainers/2.4.0/json) |
| `SQLAlchemy` | 2.1.3 | MIT | [PyPI](https://pypi.org/pypi/SQLAlchemy/2.1.3/json) |
| `starlette` | 1.7.0 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/starlette/1.7.0/json) |
| `typing_extensions` | 4.16.0 | PSF-2.0 | [PyPI](https://pypi.org/pypi/typing_extensions/4.16.0/json) |
| `typing-inspection` | 0.4.4 | MIT | [PyPI](https://pypi.org/pypi/typing-inspection/0.4.4/json) |
| `tzdata` | 2026.5 | Apache-2.0 | [PyPI](https://pypi.org/pypi/tzdata/2026.5/json) |
| `urllib3` | 2.8.0 | MIT | [PyPI](https://pypi.org/pypi/urllib3/2.8.0/json) |
| `uvicorn` | 0.54.0 | BSD-3-Clause | [PyPI](https://pypi.org/pypi/uvicorn/0.54.0/json) |
| `yarl` | 1.25.1 | Apache-2.0 | [PyPI](https://pypi.org/pypi/yarl/1.25.1/json) |

## Полные уведомления прямых frontend-зависимостей

Тексты извлечены из восьми точных npm-архивов без установки и исполнения кода.
Проверка integrity каждого архива выполнена по закреплённому lockfile. Эти тексты
сохраняют права исходных авторов. Для готовой сборки дополнительно сохраняются
уведомления фактически включённых транзитивных и native-компонентов.

### @fontsource-variable/manrope 5.3.0

Первичный источник: [package/LICENSE](https://registry.npmjs.org/@fontsource-variable/manrope/-/manrope-5.3.0.tgz). Целостность архива совпала с `package-lock.json`.

```text
Copyright 2019 The Manrope Project Authors (https://github.com/sharanda/manrope)

This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license is copied below, and is also available with a FAQ at:
http://scripts.sil.org/OFL


-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```

### @hugeicons/core-free-icons 4.3.5

Первичный источник: [package/LICENSE.md](https://registry.npmjs.org/@hugeicons/core-free-icons/-/core-free-icons-4.3.5.tgz). Целостность архива совпала с `package-lock.json`.

```text
MIT License

Copyright (c) 2025 Hugeicons

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### @hugeicons/react 1.1.10

Первичный источник: [package/LICENSE.md](https://registry.npmjs.org/@hugeicons/react/-/react-1.1.10.tgz). Целостность архива совпала с `package-lock.json`.

```text
MIT License

Copyright (c) 2025 Hugeicons

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### lucide-react 1.52.0

Первичный источник: [package/LICENSE](https://registry.npmjs.org/lucide-react/-/lucide-react-1.52.0.tgz). Целостность архива совпала с `package-lock.json`.

```text
ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

---

The following Lucide icons are derived from the Feather project:

airplay, alert-circle, alert-octagon, alert-triangle, aperture, arrow-down-circle, arrow-down-left, arrow-down-right, arrow-down, arrow-left-circle, arrow-left, arrow-right-circle, arrow-right, arrow-up-circle, arrow-up-left, arrow-up-right, arrow-up, at-sign, calendar, cast, check, chevron-down, chevron-left, chevron-right, chevron-up, chevrons-down, chevrons-left, chevrons-right, chevrons-up, circle, clipboard, clock, code, columns, command, compass, corner-down-left, corner-down-right, corner-left-down, corner-left-up, corner-right-down, corner-right-up, corner-up-left, corner-up-right, crosshair, database, divide-circle, divide-square, dollar-sign, download, external-link, feather, frown, hash, headphones, help-circle, info, italic, key, layout, life-buoy, link-2, link, loader, lock, log-in, log-out, maximize, meh, minimize, minimize-2, minus-circle, minus-square, minus, monitor, moon, more-horizontal, more-vertical, move, music, navigation-2, navigation, octagon, pause-circle, percent, plus-circle, plus-square, plus, power, radio, rss, search, server, share, shopping-bag, sidebar, smartphone, smile, square, table-2, tablet, target, terminal, trash-2, trash, triangle, tv, type, upload, x-circle, x-octagon, x-square, x, zoom-in, zoom-out

The MIT License (MIT) (for the icons listed above)

Copyright (c) 2013-present Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### motion 14.0.0

Первичный источник: [package/LICENSE.md](https://registry.npmjs.org/motion/-/motion-14.0.0.tgz). Целостность архива совпала с `package-lock.json`.

```text
The MIT License (MIT)

Copyright (c) 2024 [Motion](https://motion.dev) B.V.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### qrcode 1.5.4

Первичный источник: [package/license](https://registry.npmjs.org/qrcode/-/qrcode-1.5.4.tgz). Целостность архива совпала с `package-lock.json`.

```text
The MIT License (MIT)

Copyright (c) 2012 Ryan Day

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### react 19.3.0

Первичный источник: [package/LICENSE](https://registry.npmjs.org/react/-/react-19.3.0.tgz). Целостность архива совпала с `package-lock.json`.

```text
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### react-dom 19.3.0

Первичный источник: [package/LICENSE](https://registry.npmjs.org/react-dom/-/react-dom-19.3.0.tgz). Целостность архива совпала с `package-lock.json`.

```text
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Уведомления проверенных транзитивных frontend-компонентов

`qrcode/lib/core/segments.js` импортирует `dijkstrajs`, а production-entry
`react-dom/client` использует `scheduler`. Их точные npm-архивы дополнительно
сверены с integrity lockfile; тексты ниже сохранены без изменения. Этот список
отражает проверенные browser-entry цепочки, а не все пакеты окружения.

### dijkstrajs 1.0.3

Первичный источник: [package/LICENSE.md](https://registry.npmjs.org/dijkstrajs/-/dijkstrajs-1.0.3.tgz).
SHA-256 исходного LICENSE: `c46324e45a005413535a6fb7a97e9eacd3cc6bf30335b7d5c10b8ee3af9e60c2`.

````text
```
Dijkstra path-finding functions. Adapted from the Dijkstar Python project.

Copyright (C) 2008
  Wyatt Baldwin <self@wyattbaldwin.com>
  All rights reserved

Licensed under the MIT license.

  http://www.opensource.org/licenses/mit-license.php

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
````

### scheduler 0.28.0

Первичный источник: [package/LICENSE](https://registry.npmjs.org/scheduler/-/scheduler-0.28.0.tgz).
SHA-256 исходного LICENSE: `da6d3703ed11cbe42bd212c725957c98da23cbff1998c05fa4b3d976d1a58e93`.

```text
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Полные MIT-уведомления инструкционных навыков

Эти тексты сохраняются для локальных копий и любого будущего включения навыков
или существенных частей их текста. Они не меняют лицензию навыков на AGPL.

### github/awesome-copilot create-readme

[Первичный текст](https://raw.githubusercontent.com/github/awesome-copilot/36e95dfda72140a68f577d5c894a551607606bd3/LICENSE).

```text
MIT License

Copyright GitHub, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### blader/humanizer

[Первичный текст](https://raw.githubusercontent.com/blader/humanizer/225a6f39ac85f76ee48dbad772ea4abe4ed6c9d8/LICENSE).

```text
MIT License

Copyright (c) 2025 Siqi Chen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
