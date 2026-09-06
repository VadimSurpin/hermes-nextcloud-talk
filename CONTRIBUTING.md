# Contributing

Спасибо за интерес к проекту!

## Как помочь

- **Баги:** откройте issue с логом `gateway.log` (фрагмент вокруг ошибки), версией Nextcloud/Talk и шагами воспроизведения.
- **Фичи:** сначала issue с описанием сценария — обсудим дизайн до кода.
- **PR:** форк → ветка `feat/<имя>` → PR. Держите изменения атомарными.

## Среда разработки

```bash
# Структура плагина Hermes
~/.hermes/plugins/nextcloud-talk/
├── __init__.py     # from .adapter import register
├── adapter.py
└── plugin.yaml

# Быстрый smoke-тест вне gateway
import sys; sys.path.insert(0, '/opt/hermes')
from hermes_cli.plugins import discover_plugins
discover_plugins()
from gateway.platform_registry import platform_registry
adapter = platform_registry.create_adapter('talk', config)
```

## Стиль кода

- Python 3.12+, type hints приветствуются
- Логи через модульный `logging.getLogger(__name__)`
- Все вызовы OCS API — через `_get`/`_post`/`_ocs_api_raw` (единая auth-обработка)
- Блокирующие операции (WebDAV, ffmpeg) — только через `run_in_executor`

## Тест-чеклист перед PR

- [ ] Текст: отправка и приём (long-poll ловит сообщение другого пользователя)
- [ ] Голосовое OGG → компактный waveform-плеер (проверить `messageType: voice-message` в истории)
- [ ] Картинка/видео с caption — подпись на медиа, одним сообщением
- [ ] Фолбэк share-as-file не ломается при отключённом attachment API
- [ ] `hermes gateway restart` проходит без `KeyError: 'platform'`

## Коммиты

Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:` — на русском или английском.
