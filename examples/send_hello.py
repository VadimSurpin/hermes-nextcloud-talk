"""Пример standalone-использования адаптера (без gateway).

Запуск:
    TALK_SERVER_URL=https://nc.example.com \
    TALK_USER=hermes TALK_APP_PASSWORD=xxx \
    TALK_ROOM_TOKEN=abcd1234 \
    python examples/send_hello.py
"""
import asyncio
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from hermes_cli.plugins import discover_plugins  # noqa: E402


async def main():
    discover_plugins()

    from gateway.config import Platform, PlatformConfig
    from gateway.platform_registry import platform_registry

    extra = {
        "server": os.environ["TALK_SERVER_URL"],
        "user": os.environ["TALK_USER"],
        "app_password": os.environ["TALK_APP_PASSWORD"],
        "room_token": os.environ["TALK_ROOM_TOKEN"],
    }
    cfg = PlatformConfig(enabled=True, extra=extra)
    adapter = platform_registry.create_adapter("talk", cfg)

    ok = await adapter.connect()
    print("connect:", ok)

    room = os.environ["TALK_ROOM_TOKEN"]

    # Текст
    r = await adapter.send(room, "👋 Привет из standalone-примера!")
    print("text:", r.success)

    # Голосовое (если есть ffmpeg и ogg-файл)
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        r = await adapter.send_voice(room, sys.argv[1], caption="🎙 Пример голосового")
        print("voice:", r.success)

    await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
