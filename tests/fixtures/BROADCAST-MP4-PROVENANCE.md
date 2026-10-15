# Происхождение тестового MP4

`broadcast-chromium-synthetic.mp4` — локально созданный координатором 11 октября 2026 ролик: canvas 64×64, шесть одноцветных кадров с периодом 200 ms, Chromium `MediaRecorder`, `video/mp4;codecs=avc1.42001E`. Исходные цвета — `#204060` и `#6080a0`. Съёмки камеры, клиентских данных, брендинга и внешних загрузок нет.

Координатор выполнил локальное декодирование этого конкретного файла: 64×64, длительность 1.099467 s, 1403 байта. Воспроизводимый генератор сохранён в закрытом локальном `.private/runtime/generate_broadcast_fixture.js`; исходный артефакт — `.artifacts/broadcast-chromium-synthetic.mp4`. Исполнитель загрузчика не запускал браузер и не присваивает себе проверку координатора.

Контейнер содержит `ftyp`, `moov`, `moof`, `mdat`, `mfra`. Эта фикстура проверяет поддержку настоящего fragmented MP4; структурная приёмка других файлов не подтверждает декодирование любого видео или совместимость конкретного клиента Telegram.

SHA256 фикстуры: `A4B549ECF373A99D33DF297713BF5B3D02968851F6089E25CBF1A3CE64AC07F0`.

Структурный профиль fragmented MP4 сверялся с [W3C ISO BMFF Byte Stream Format](https://www.w3.org/TR/mse-byte-stream-format-isobmff/): movie-fragment relative addressing, `mvex`, `traf`, `tfdt`, локальные data references. Фундамент не является полноценным ISO BMFF декодером.
