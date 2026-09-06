"""Tests for the Nextcloud Talk adapter (run: pytest -v tests/).

Requires a live Nextcloud instance — set these environment variables:
  TALK_TEST_SERVER, TALK_TEST_USER, TALK_TEST_PASSWORD, TALK_TEST_ROOM
Without them, integration tests are skipped.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapter import NextcloudTalkAdapter, _guess_ctype  # noqa: E402

HAS_CREDS = all(os.getenv(v) for v in (
    "TALK_TEST_SERVER", "TALK_TEST_USER", "TALK_TEST_PASSWORD", "TALK_TEST_ROOM"))

pytestmark = pytest.mark.skipif(not HAS_CREDS, reason="TALK_TEST_* env not set")


def make_adapter():
    from gateway.config import Platform, PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={
        "server": os.environ["TALK_TEST_SERVER"],
        "user": os.environ["TALK_TEST_USER"],
        "app_password": os.environ["TALK_TEST_PASSWORD"],
        "room_token": os.environ["TALK_TEST_ROOM"],
    })
    return NextcloudTalkAdapter(cfg, Platform("talk"))


# ---------- unit (no network) ----------

def test_guess_ctype():
    assert _guess_ctype("a.ogg") == "audio/ogg"
    assert _guess_ctype("b.MP3") == "audio/mpeg"
    assert _guess_ctype("c.png") == "image/png"
    assert _guess_ctype("d.xyz") == "application/octet-stream"


# ---------- integration (live Nextcloud) ----------

def test_connect_disconnect():
    a = make_adapter()
    assert asyncio.run(a.connect()) is True
    asyncio.run(a.disconnect())


def test_send_text():
    a = make_adapter()
    asyncio.run(a.connect())
    room = os.environ["TALK_TEST_ROOM"]
    r = asyncio.run(a.send(room, "pytest: send test ✅"))
    asyncio.run(a.disconnect())
    assert r.success


def test_send_voice_creates_voice_message():
    """Key contract: mimeType mp3 + talkMetaData → messageType=voice-message."""
    a = make_adapter()
    asyncio.run(a.connect())
    room = os.environ["TALK_TEST_ROOM"]
    # Generate a 1-second mp3 if no fixture exists
    import subprocess, tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", tmp.name], check=True)
    r = asyncio.run(a.send_voice(room, tmp.name))
    asyncio.run(a.disconnect())
    assert r.success

    # Verify the last message
    import json, urllib.request, base64
    pwd = os.environ["TALK_TEST_PASSWORD"]
    b64 = base64.b64encode(f"{os.environ['TALK_TEST_USER']}:{pwd}".encode()).decode()
    req = urllib.request.Request(
        f"{os.environ['TALK_TEST_SERVER'].rstrip('/')}"
        f"/ocs/v2.php/apps/spreed/api/v1/chat/{room}?lookIntoFuture=0&limit=1",
        headers={"OCS-APIRequest": "true", "Accept": "application/json",
                 "Authorization": "Basic " + b64})
    d = json.load(urllib.request.urlopen(req, timeout=20))
    last = d["ocs"]["data"][0]
    assert last["messageType"] == "voice-message", f"expected voice-message, got {last['messageType']}"


def test_send_file_image_with_caption():
    a = make_adapter()
    asyncio.run(a.connect())
    room = os.environ["TALK_TEST_ROOM"]
    png = Path(__file__).parent / "fixture_1px.png"
    if not png.exists():
        png.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000d49444154789c626001000000ffff030000060005"
            "57bfabd40000000049454e44ae426082"))
    r = asyncio.run(a.send_file(room, str(png), caption="pytest caption"))
    asyncio.run(a.disconnect())
    assert r.success
