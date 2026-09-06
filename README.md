# Nextcloud Talk Platform Adapter for Hermes Agent

Плагин-платформа [Hermes Agent](https://hermes-agent.nousresearch.com), превращающий **Nextcloud Talk** в полноценный канал общения: текст, голосовые (компактный waveform-плеер), картинки/видео/файлы с подписями.

## Возможности

| Функция | Реализация |
|---|---|
| Приём сообщений | Long-poll `GET /chat/{token}?lookIntoFuture=1` |
| Отправка текста | `POST /chat/{token}` |
| 🎙 Голосовые (нативные) | OGG → MP3 (ffmpeg) → Draft-folder → `attachment` c `messageType: voice-message` → компактный waveform-плеер |
| 🖼 Картинки / 🎬 Видео / файлы | Draft-folder → `attachment` c `caption` в `talkMetaData` — подпись на самом медиа |
| Фолбэк | Share-to-chat (`shareType=10`) при недоступности attachment-endpoint |
| Cron-доставка | `deliver=talk` + `TALK_HOME_CHANNEL` |
| Сессии/память/скиллы | Полный стек Hermes |

## Установка

Скопируйте каталог в `~/.hermes/plugins/` (или `$HERMES_HOME/plugins/`):

```
~/.hermes/plugins/nextcloud-talk/
├── __init__.py   # from .adapter import register
├── adapter.py
└── plugin.yaml
```

Включите плагин и платформу в `config.yaml`:

```yaml
plugins:
  enabled:
    - nextcloud-talk-platform

platforms:
  talk:
    enabled: true
    extra:
      server: https://nextcloud.example.com
      user: hermes
      app_password: <app-password>
      room_token: <room-token>       # комната по умолчанию
    home_channel:
      platform: talk                 # ОБЯЗАТЕЛЬНО ровно эти 3 ключа
      chat_id: <room-token>
      name: Hermes ↔ User
```

Перезапустите gateway: `hermes gateway restart`

⚠️ **Важно:** `home_channel` должен содержать ровно ключи `platform`, `chat_id`, `name` — иначе gateway падает при старте (`KeyError: 'platform'` в `HomeChannel.from_dict`).

## Протокол (кратко)

- **Транспорт:** OCS REST (`OCS-APIRequest: true`, Basic auth: user + app-password)
- **Приём:** long-poll с `lastKnownMessageId`; собственные сообщения фильтруются по `actorId`
- **Голосовые:** Talk принимает метку `voice-message` **только** для `audio/mpeg` | `audio/wav` — поэтому OGG конвертируется в MP3 64k
- **Медиа:** `POST /chat/{token}/attachment/folder` → WebDAV PUT в Draft → `POST /chat/{token}/attachment` с `talkMetaData` (caption/messageType)

## Pairing

Первое сообщение пользователя возвращает код; подтвердите:

```bash
hermes pairing approve talk <CODE>
```

## Требования

- Nextcloud с Talk ≥ 17 (attachment API), Hermes Agent
- `httpx` (уже входит в Hermes), `ffmpeg` в PATH (для голосовых)

## Лицензия

MIT
