# Nextcloud Talk Platform Adapter for Hermes Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Hermes Agent](https://img.shields.io/badge/Platform-Hermes_Agent-black)](https://hermes-agent.nousresearch.com)
[![Nextcloud Talk](https://img.shields.io/badge/Nextcloud_Talk-%E2%89%A517-blue)](https://github.com/nextcloud/spreed)

A Hermes Agent platform plugin that turns **Nextcloud Talk** into a full-featured communication channel: text, native voice messages with a compact waveform player, images/videos/files with captions — just like Telegram.

## Features

| Feature | Implementation |
|---|---|
| Message receiving | Long-poll `GET /chat/{token}?lookIntoFuture=1`, own messages auto-filtered |
| Text sending | `POST /chat/{token}` |
| 🎙 Native voice messages | OGG → MP3 (ffmpeg) → Draft-folder → attachment endpoint with `messageType: voice-message` → compact waveform player |
| 🖼 Images / 🎬 Videos / files | Attachment endpoint + `caption` in `talkMetaData` — caption lives on the media itself |
| 📎 Delivery fallback | Share-to-chat (`shareType=10`) when the attachment endpoint is unavailable |
| ⏰ Cron delivery | `deliver=talk` + `TALK_HOME_CHANNEL` |
| 🧠 Full Hermes stack | Sessions, memory, skills, pairing — everything works |

## Installation

### 1. Copy the plugin

```bash
mkdir -p ~/.hermes/plugins/nextcloud-talk
cp adapter.py plugin.yaml __init__.py ~/.hermes/plugins/nextcloud-talk/
```

Structure:

```
~/.hermes/plugins/nextcloud-talk/
├── __init__.py   # from .adapter import register
├── adapter.py
└── plugin.yaml
```

### 2. Configure config.yaml

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
      room_token: <room-token>       # default room
    home_channel:
      platform: talk                 # MUST be exactly these 3 keys
      chat_id: <room-token>
      name: Hermes ↔ User
```

### 3. Restart the gateway

```bash
hermes gateway restart
```

### 4. Pairing

The user's first message returns a confirmation code:

```bash
hermes pairing approve talk <CODE>
```

## Creating an App Password

Nextcloud → **Settings → Security → Devices & sessions** → "Create new app password". Use this password (not your main one!) as `app_password`.

The room token is the last segment of the room URL in the Talk web UI: `https://nc.example.com/call/<token>`.

## Architecture / protocol

```
Hermes gateway ──listen──▶ GET /ocs/v2.php/apps/spreed/api/v1/chat/{token}
                           ?lookIntoFuture=1&lastKnownMessageId=N (long-poll 30s)
        │
        └──send───▶ POST /chat/{token}                                  (text)
                    POST /chat/{token}/attachment/folder                (probe Draft)
                    WebDAV PUT /Draft/<uuid>.<ext>                      (file)
                    POST /chat/{token}/attachment                       (media+caption)
```

### Gotchas discovered during development

- **Voice-message mime:** Talk only accepts `messageType: voice-message` for `audio/mpeg` and `audio/wav`. OGG (opus) silently loses the label and renders as a large file player → the adapter converts OGG → MP3 64k via ffmpeg.
- **`referenceId` is required** for the attachment endpoint — without it you get HTTP 400.
- **`home_channel`** must contain exactly `{platform, chat_id, name}` — otherwise the gateway crashes on startup (`KeyError: 'platform'` in `HomeChannel.from_dict`). Verified by an actual crash-loop.
- **Pairing is mandatory:** without approval, user messages are ignored (`Unauthorized user` in the log).

## Environment variables

| Variable | Description |
|---|---|
| `TALK_SERVER_URL` | Nextcloud base URL |
| `TALK_USER` | Bot user |
| `TALK_APP_PASSWORD` | App password |
| `TALK_ROOM_TOKEN` | Default room |
| `TALK_HOME_CHANNEL` | Room for cron delivery |
| `TALK_POLL_INTERVAL` | Seconds between polls (default 2) |

## Limitations

- Voice messages are **sent** (upload) fine, but incoming voice messages arrive as a text placeholder — transcription can be added separately via [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
- No reactions/threads (Talk API provides them; the adapter doesn't map them yet).
- One long-poll per room: many rooms = more open connections.

## Debugging

```bash
tail -f ~/.hermes/logs/gateway.log | grep -i talk
hermes gateway status
```

Common issues:

| Symptom | Cause |
|---|---|
| Gateway won't start, `KeyError: 'platform'` | Invalid `home_channel` — see Installation |
| `Unauthorized user` in the log | Run `hermes pairing approve talk <CODE>` |
| Voice message renders as a file | Old Talk version (<17) without the attachment API |
| `talk connected` but no messages arrive | Make sure you're writing to the room from `room_token` |

## Requirements

- Hermes Agent
- Nextcloud + Talk ≥ 17 (attachment API)
- Python: `httpx` (already a Hermes dependency)
- System: `ffmpeg` (for voice conversion)

## License

[MIT](LICENSE) © 2026 Vadim Surpin
