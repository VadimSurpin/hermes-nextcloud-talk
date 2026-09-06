# Nextcloud Talk Platform Adapter for Hermes Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Hermes Agent](https://img.shields.io/badge/Platform-Hermes_Agent-black)](https://hermes-agent.nousresearch.com)
[![Nextcloud Talk](https://img.shields.io/badge/Nextcloud_Talk-%E2%89%A517-blue)](https://github.com/nextcloud/spreed)

Плагин-платформа, превращающий **Nextcloud Talk** в полноценный канал общения с Hermes Agent: текст, нативные голосовые с waveform-плеером, картинки/видео/файлы с подписями — как в Telegram.

## Возможности

| Функция | Реализация |
|---|---|
| Приём сообщений | Long-poll `GET /chat/{token}?lookIntoFuture=1`, автофильтр собственных сообщений |
| Отправка текста | `POST /chat/{token}` |
| 🎙 Голосовые (нативные) | OGG → MP3 (ffmpeg) → Draft-folder → attachment endpoint → компактный waveform-плеер |
| 🖼 Картинки / 🎬 Видео / файлы | Attachment endpoint + `caption` в `talkMetaData` — подпись на самом медиа |
| 📎 Фолбэк доставки | Share-to-chat (`shareType=10`) при недоступности attachment-endpoint |
| ⏰ Cron-доставка | `deliver=talk` + переменная `TALK_HOME_CHANNEL` |
| 🧠 Стек Hermes | Сессии, память, скиллы, pairing — всё работает |

## Установка

### 1. Скопируйте плагин

```bash
mkdir -p ~/.hermes/plugins/nextcloud-talk
cp adapter.py plugin.yaml __init__.py ~/.hermes/plugins/nextcloud-talk/
```

Структура:

```
~/.hermes/plugins/nextcloud-talk/
├── __init__.py   # from .adapter import register
├── adapter.py
└── plugin.yaml
```

### 2. Настройте config.yaml

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

### 3. Перезапустите gateway

```bash
hermes gateway restart
```

### 4. Pairing

Первое сообщение пользователя вернёт код подтверждения:

```bash
hermes pairing approve talk <CODE>
```

## Создание App Password

Nextcloud → **Настройки → Безопасность → Устройства и сессии** → «Создать новый пароль приложения». Используйте этот пароль (не основной!) в `app_password`.

Токен комнаты можно получить из URL комнаты в веб-интерфейсе Talk: `https://nc.example.com/call/<token>`.

## Архитектура / протокол

```
Hermes gateway ──listen──▶ GET /ocs/v2.php/apps/spreed/api/v1/chat/{token}
                           ?lookIntoFuture=1&lastKnownMessageId=N (long-poll 30s)
        │
        └──send───▶ POST /chat/{token}                                  (текст)
                    POST /chat/{token}/attachment/folder                (probe Draft)
                    WebDAV PUT /Draft/<uuid>.<ext>                      (файл)
                    POST /chat/{token}/attachment                       (media+caption)
```

### Нюансы, обнаруженные при разработке

- **Voice-message mime:** Talk принимает `messageType: voice-message` только для `audio/mpeg` и `audio/wav`. OGG (opus) молча теряет метку и рендерится большим файловым плеером → адаптер конвертирует OGG → MP3 64k через ffmpeg.
- **`referenceId` обязателен** для attachment endpoint — без него HTTP 400.
- **`home_channel`** должен содержать ровно `{platform, chat_id, name}` — иначе gateway падает при старте (`KeyError: 'platform'` в `HomeChannel.from_dict`). Проверено crash-loop'ом.
- **Pairing** обязателен: без одобрения сообщения пользователя игнорируются (`Unauthorized user` в логе).

## Конфигурация через переменные окружения

| Переменная | Описание |
|---|---|
| `TALK_SERVER_URL` | Базовый URL Nextcloud |
| `TALK_USER` | Пользователь бота |
| `TALK_APP_PASSWORD` | App password |
| `TALK_ROOM_TOKEN` | Комната по умолчанию |
| `TALK_HOME_CHANNEL` | Комната для cron-доставки |
| `TALK_POLL_INTERVAL` | Секунды между опросами (по умолчанию 2) |

## Ограничения

- Голосовые **отправляются** (upload), но входящие voice-сообщения пока приходят как текст-заглушка — расшифровку можно подключить через [faster-whisper](https://github.com/SYSTRAN/faster-whisper) отдельно.
- Нет реакций/тредов (Talk API их отдаёт, адаптер пока не маппит).
- 1 long-poll на комнату: много комнат = больше открытых соединений.

## Отладка

```bash
tail -f ~/.hermes/logs/gateway.log | grep -i talk
hermes gateway status
```

Типовые проблемы:

| Симптом | Причина |
|---|---|
| Gateway не стартует, `KeyError: 'platform'` | Невалидный `home_channel` — см. раздел Установка |
| `Unauthorized user` в логе | Нужен `hermes pairing approve talk <CODE>` |
| Голосовое отображается как файл | Старая версия Talk (<17) без attachment API |
| `talk connected`, но сообщений нет | Проверьте, что пишете в комнату из `room_token` |

## Требования

- Hermes Agent
- Nextcloud + Talk ≥ 17 (attachment API)
- Python: `httpx` (уже входит в Hermes)
- Системно: `ffmpeg` (для конвертации голосовых)

## Лицензия

[MIT](LICENSE) © 2026 Vadim Surpin
