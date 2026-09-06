# Changelog

Все заметные изменения документируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [1.0.0] — 2026-09-06

### Added
- Платформенный адаптер Nextcloud Talk для Hermes Agent (plugin API, `register(ctx)`).
- Приём сообщений: long-poll `GET /chat/{token}?lookIntoFuture=1` с фильтром собственных сообщений по `actorId`.
- Отправка текста: `POST /chat/{token}`.
- Нативные голосовые сообщения: probe Draft-folder → WebDAV upload → attachment endpoint с `messageType: voice-message`; авто-конвертация OGG → MP3 (ffmpeg, 64k) — Talk принимает метку только для `audio/mpeg`/`audio/wav`.
- Медиа и файлы (картинки/видео/документы): attachment endpoint с `caption` в `talkMetaData` — подпись на самом медиа.
- Фолбэк-доставка: WebDAV в `Files/Talk` + share-to-chat (`shareType=10`).
- Cron-доставка: `deliver=talk`, переменная `TALK_HOME_CHANNEL`, standalone-sender для out-of-process cron.
- Env-enablement: автоконфигурация из переменных `TALK_*` до создания адаптера.
- Pairing-интеграция (`hermes pairing approve talk <CODE>`).
- Документация: README с установкой, протоколом, отладкой; примеры типовых проблем.
