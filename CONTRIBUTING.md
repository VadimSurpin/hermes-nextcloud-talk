# Contributing

Thanks for your interest in this project!

## How to help

- **Bugs:** open an issue with a `gateway.log` excerpt (around the error), your Nextcloud/Talk versions, and reproduction steps.
- **Features:** start with an issue describing the use case — let's discuss the design before the code.
- **PRs:** fork → branch `feat/<name>` → PR. Keep changes atomic.

## Development setup

```bash
# Hermes plugin layout
~/.hermes/plugins/nextcloud-talk/
├── __init__.py     # from .adapter import register
├── adapter.py
└── plugin.yaml

# Quick smoke test outside the gateway
import sys; sys.path.insert(0, '/opt/hermes')
from hermes_cli.plugins import discover_plugins
discover_plugins()
from gateway.platform_registry import platform_registry
adapter = platform_registry.create_adapter('talk', config)
```

## Code style

- Python 3.12+, type hints welcome
- Logging via module-level `logging.getLogger(__name__)`
- All OCS API calls go through `_get`/`_post`/`_ocs_api_raw` (unified auth handling)
- Blocking operations (WebDAV, ffmpeg) — only via `run_in_executor`

## Pre-PR test checklist

- [ ] Text: send and receive (long-poll picks up another user's message)
- [ ] Voice OGG → compact waveform player (verify `messageType: voice-message` in history)
- [ ] Image/video with caption — caption on the media, single message
- [ ] Share-as-file fallback doesn't break when the attachment API is disabled
- [ ] `hermes gateway restart` completes without `KeyError: 'platform'`

## Commits

Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:` — English preferred.
