"""Nextcloud Talk platform adapter (Hermes plugin)."""

import asyncio
import json
import logging
import os
import time
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

log = logging.getLogger(__name__)

API_V1 = "/ocs/v2.php/apps/spreed/api/v1"
API_V4 = "/ocs/v2.php/apps/spreed/api/v4"
MAX_MESSAGE_LENGTH = 32000
TALK_FILES_DIR = "Talk"          # Files/Talk — chat attachment storage


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v else default


class NextcloudTalkAdapter(BasePlatformAdapter):
    def __init__(self, cfg: PlatformConfig):
        from gateway.config import Platform
        super().__init__(config=cfg, platform=Platform("talk"))
        extra = getattr(cfg, "extra", None) or {}
        self.server = (_env("TALK_SERVER_URL") or extra.get("server") or "").rstrip("/")
        self.user = _env("TALK_USER") or extra.get("user", "")
        self.password = _env("TALK_APP_PASSWORD") or extra.get("app_password", "")
        self.default_room = _env("TALK_ROOM_TOKEN") or extra.get("room_token") or ""
        self.poll_interval = float(_env("TALK_POLL_INTERVAL") or extra.get("poll_interval") or 2)
        self._client: Optional[httpx.AsyncClient] = None
        self._last_msg_ids: Dict[str, int] = {}
        self._processed_ids: Dict[str, set] = {}   # chat -> set(msg ids) для идемпотентности
        self._running = False
        self._listen_task: Optional[asyncio.Task] = None
        self._rooms_refresh_at = 0.0               # когда пересобрать список комнат
        self._room_tasks: Dict[str, asyncio.Task] = {}  # token -> task (параллельный поллинг)

    # ---------- HTTP helpers ----------

    def _headers(self) -> Dict[str, str]:
        import base64
        cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return {
            "OCS-APIRequest": "true",
            "Accept": "application/json",
            "Authorization": f"Basic {cred}",
        }

    def _url(self, path: str) -> str:
        return f"{self.server}{path}"

    async def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        r = await self._client.get(self._url(path), params=params, headers=self._headers())
        r.raise_for_status()
        return r.json().get("ocs", {}).get("data", {})

    async def _post(self, path: str, payload: Dict) -> Any:
        r = await self._client.post(self._url(path), json=payload, headers=self._headers())
        r.raise_for_status()
        return r.json().get("ocs", {}).get("data", {})

    # ---------- BasePlatformAdapter ----------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not HTTPX_AVAILABLE:
            log.error("httpx not installed — Talk adapter unavailable")
            return False
        if not (self.server and self.user and self.password):
            log.error("TALK_SERVER_URL / TALK_USER / TALK_APP_PASSWORD not set")
            return False
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(65.0))
        rooms = await self._get(f"{API_V4}/room")
        log.info("Nextcloud Talk connected, rooms available: %d", len(rooms))
        if self.default_room:
            try:
                hist = await self._get(f"{API_V1}/chat/{self.default_room}",
                                       {"lookIntoFuture": 0, "limit": 1})
                last = hist[-1] if isinstance(hist, list) and hist else {}
                if last.get("id"):
                    self._last_msg_ids[self.default_room] = last["id"]
            except Exception as e:
                log.warning("failed to get last id: %s", e)
        self._running = True
        self._listen_task = asyncio.create_task(self.listen())
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _poll_room_forever(self, token: str) -> None:
        """Долгий поллинг одной комнаты: параллельно с другими комнатами.
        Каждая комната держит СВОЙ long-poll — задержка не растёт с числом комнат."""
        streak = 0
        while self._running:
            try:
                params = {"lookIntoFuture": 1, "limit": 30, "timeout": 30}
                last = self._last_msg_ids.get(token)
                if last:
                    params["lastKnownMessageId"] = last
                data = await self._get(f"{API_V1}/chat/{token}", params)
                streak = 0
                msgs = data if isinstance(data, list) else []
                for m in msgs:
                    mid = m.get("id", 0)
                    self._last_msg_ids[token] = max(self._last_msg_ids.get(token, 0), mid)
                    if m.get("actorId") == self.user:
                        continue
                    processed = self._processed_ids.setdefault(token, set())
                    if mid in processed:
                        continue
                    processed.add(mid)
                    if len(processed) > 500:
                        self._processed_ids[token] = set(sorted(processed)[-200:])
                    mtype = MessageType.VOICE if m.get("messageType") == "voice-message" else MessageType.TEXT
                    actor = m.get("actorId") or "unknown"
                    source = self.build_source(
                        chat_id=token, chat_name=token, chat_type="dm",
                        user_id=actor, user_name=m.get("actorDisplayName") or actor,
                    )
                    ev = MessageEvent(
                        text=m.get("message") or "",
                        message_type=mtype,
                        source=source,
                        user_id=actor,
                        user_name=m.get("actorDisplayName") or actor,
                        message_id=str(mid),
                        raw_message=m,
                        reply_to_message_id=(str(m["replyTo"]) if m.get("replyTo") else None),
                    )
                    ev.metadata = {
                        "room": token,
                        "thread_id": m.get("threadId"),
                        "reactions": m.get("reactions") or {},
                        "expiration": m.get("expirationTimestamp"),
                    }
                    await self.handle_message(ev)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (304, 404):
                    log.warning("poll %s: %s", token, e)
                    streak += 1
            except Exception as e:
                log.warning("poll %s: %s", token, e)
                streak += 1
            # backoff: обычно сразу снова long-poll (~32 с сам по себе),
            # но после ошибок — экспоненциальная пауза
            if streak:
                delay = min(self.poll_interval * (2 ** min(streak, 6)), 300.0)
                await asyncio.sleep(delay)
            # иначе — немедленно следующий long-poll

    def _spawn_room_tasks(self, tokens: list) -> None:
        """Создаёт task на каждую новую комнату (существующие не трогаем)."""
        for token in tokens:
            if token not in self._room_tasks or self._room_tasks[token].done():
                self._room_tasks[token] = asyncio.create_task(
                    self._poll_room_forever(token))

    async def listen(self) -> None:
        rooms_refresh_interval = 300.0   # пересобирать список комнат раз в 5 минут

        # стартовый набор: default_room или список комнат
        if self.default_room:
            self._spawn_room_tasks([self.default_room])
        else:
            try:
                rooms = await self._get(f"{API_V4}/room")
                self._spawn_room_tasks([r["token"] for r in rooms])
            except Exception as e:
                log.error("failed to list rooms: %s", e)

        while self._running:
            # --- пересбор списка комнат: новые комнаты получают свой task ---
            now = time.time()
            if not self.default_room and now >= self._rooms_refresh_at:
                try:
                    rooms = await self._get(f"{API_V4}/room")
                    self._spawn_room_tasks([r["token"] for r in rooms])
                    self._rooms_refresh_at = now + rooms_refresh_interval
                except Exception as e:
                    log.error("failed to list rooms: %s", e)
                    self._rooms_refresh_at = now + 15.0
            await asyncio.sleep(2)

        # остановка: все room-таски
        for t in self._room_tasks.values():
            t.cancel()
        for t in self._room_tasks.values():
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def send(self, chat_id: str, content: str,
                   reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Send text. metadata supports: thread_id (reply in thread),
        thread_title (create a new thread), silent."""
        try:
            payload: Dict[str, Any] = {"message": content[:MAX_MESSAGE_LENGTH]}
            if reply_to:
                payload["replyTo"] = int(reply_to)
            md = metadata or {}
            if md.get("thread_id"):
                payload["threadId"] = int(md["thread_id"])
            if md.get("thread_title"):
                payload["threadTitle"] = str(md["thread_title"])[:64]
            if md.get("silent"):
                payload["silent"] = True
            await self._post(f"{API_V1}/chat/{chat_id}", payload)
            return SendResult(success=True)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    # ---------- Reactions ----------

    async def react(self, chat_id: str, message_id: str,
                    emoji: str) -> SendResult:
        """Add a reaction to a message (POST /reaction/{token}/{messageId})."""
        try:
            await self._post(f"{API_V1}/reaction/{chat_id}/{int(message_id)}",
                             {"reaction": emoji})
            return SendResult(success=True)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def unreact(self, chat_id: str, message_id: str,
                      emoji: str) -> SendResult:
        """Remove a reaction (DELETE /reaction/{token}/{messageId})."""
        try:
            r = await self._client.delete(
                self._url(f"{API_V1}/reaction/{chat_id}/{int(message_id)}"),
                json={"reaction": emoji}, headers=self._headers())
            r.raise_for_status()
            return SendResult(success=True)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def get_reactions(self, chat_id: str, message_id: str) -> Dict[str, List[Dict]]:
        """Reactions of a message: {emoji: [{actorId, actorDisplayName, ...}]}."""
        data = await self._get(f"{API_V1}/reaction/{chat_id}/{int(message_id)}")
        return data if isinstance(data, dict) else {}

    # ---------- Media: files upload + share-to-chat ----------

    def _webdav_put_sync(self, remote_path: str, data: bytes, ctype: str) -> int:
        """WebDAV PUT (sync; call from to_thread). Returns HTTP status."""
        import base64
        cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req = urllib.request.Request(
            f"{self.server}/remote.php/dav/files/{self.user}{remote_path}",
            data=data, method="PUT",
            headers={"Authorization": f"Basic {cred}", "Content-Type": ctype})
        resp = urllib.request.urlopen(req, timeout=120)
        return resp.status

    def _webdav_put_file(self, remote_path: str, file_path: str, ctype: str) -> int:
        """WebDAV PUT прямо из файла — без чтения всего файла в RAM (стриминг)."""
        import base64
        cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        with open(file_path, "rb") as fh:
            req = urllib.request.Request(
                f"{self.server}/remote.php/dav/files/{self.user}{remote_path}",
                data=fh, method="PUT",
                headers={"Authorization": f"Basic {cred}", "Content-Type": ctype})
            resp = urllib.request.urlopen(req, timeout=600)
            return resp.status


    def _webdav_fileid_sync(self, remote_path: str) -> Optional[str]:
        """PROPFIND fileid (sync)."""
        import base64
        cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        propfind = ('<?xml version="1.0"?>'
                    '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                    '<d:prop><oc:fileid/></d:prop></d:propfind>')
        req = urllib.request.Request(
            f"{self.server}/remote.php/dav/files/{self.user}{remote_path}",
            data=propfind.encode(), method="PROPFIND",
            headers={"Authorization": f"Basic {cred}",
                     "Content-Type": "application/xml", "Depth": "0"})
        body = urllib.request.urlopen(req, timeout=30).read().decode()
        import re
        m = re.search(r"<oc:fileid>(\d+)</oc:fileid>", body)
        return m.group(1) if m else None

    def _ocs_share_to_room_sync(self, remote_path: str, room_token: str) -> bool:
        """files_sharing API: shareType=10 (Talk room)."""
        import base64
        cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req = urllib.request.Request(
            f"{self.server}/ocs/v2.php/apps/files_sharing/api/v1/shares",
            data=json.dumps({"path": remote_path, "shareType": 10,
                             "shareWith": room_token}).encode(),
            headers={"OCS-APIRequest": "true", "Accept": "application/json",
                     "Content-Type": "application/json",
                     "Authorization": f"Basic {cred}"})
        resp = urllib.request.urlopen(req, timeout=30)
        return 200 <= resp.status < 300

    async def _ensure_talk_dir(self) -> None:
        """Creates Files/Talk if missing (MKCOL idempotent)."""
        import base64
        cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req = urllib.request.Request(
            f"{self.server}/remote.php/dav/files/{self.user}/{TALK_FILES_DIR}/",
            method="MKCOL",
            headers={"Authorization": f"Basic {cred}", "Content-Type": "application/xml"})
        try:
            await asyncio.to_thread(
                lambda: urllib.request.urlopen(req, timeout=20).read())
        except Exception:
            pass  # 405 = already exists

    async def send_file(self, chat_id: str, file_path: str,
                        caption: Optional[str] = None,
                        reply_to: Optional[str] = None,
                        metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Media/file: Draft-folder → WebDAV → attachment endpoint
        (caption inside talkMetaData — caption on the media itself, like Telegram).
        Fallback: WebDAV to Files/Talk + share-to-chat, caption as a separate message."""
        try:
            fname = os.path.basename(file_path)

            # Primary path: attachment endpoint (caption on media)
            try:
                code, r = await asyncio.to_thread(lambda: self._ocs_api_raw(
                    f"{API_V1}/chat/{chat_id}/attachment/folder", {"fileNames": [fname]}))
                if code == 200:
                    folder = r["ocs"]["data"]["folder"]
                    tmp_name = f"{uuid.uuid4().hex}{os.path.splitext(fname)[1]}"
                    ctype = self._guess_ctype(file_path)
                    await asyncio.to_thread(lambda: self._webdav_put_file(
                        f"/{folder}/{tmp_name}", file_path, ctype))
                    meta: Dict[str, Any] = {}
                    if caption:
                        meta["caption"] = caption[:1000]
                    code2, r2 = await asyncio.to_thread(lambda: self._ocs_api_raw(
                        f"{API_V1}/chat/{chat_id}/attachment", {
                            "filePath": f"{folder}/{tmp_name}",
                            "referenceId": uuid.uuid4().hex,
                            "talkMetaData": json.dumps(meta),
                            "fileName": fname,
                        }))
                    if code2 == 200:
                        return SendResult(success=True)
                    log.warning("[talk] attachment %s — falling back to share-as-file", code2)
            except Exception as e:
                log.warning("[talk] attachment path failed: %s — fallback", e)

            # Fallback: Files/Talk + share-to-chat
            await self._ensure_talk_dir()
            stamp = int(time.time())
            safe = f"{stamp}_{fname}"
            remote = f"/{TALK_FILES_DIR}/{safe}"
            ctype = self._guess_ctype(file_path)
            status = await asyncio.to_thread(
                lambda: self._webdav_put_file(remote, file_path, ctype))
            if status not in (200, 201, 204):
                return SendResult(success=False, error=f"upload http {status}")
            await asyncio.to_thread(
                lambda: self._ocs_share_to_room_sync(remote, chat_id))
            if caption:
                await self.send(chat_id, caption[:500], reply_to=reply_to)
            return SendResult(success=True)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_voice(self, chat_id: str, audio_path: str,
                         caption: Optional[str] = None,
                         reply_to: Optional[str] = None,
                         metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        """Native voice-message (compact waveform player).
        Talk only accepts the voice-message label for audio/mpeg | audio/wav,
        so OGG is converted to MP3 (ffmpeg). Fallback: share-as-file."""
        tmp_converted = None
        try:
            send_path = audio_path
            ext = os.path.splitext(audio_path)[1].lower()
            if ext not in (".mp3", ".wav"):
                # convert to mp3 (voice-message only allows mpeg/wav)
                import subprocess, tempfile
                tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                tmp.close()
                proc = await asyncio.to_thread(lambda: subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", audio_path,
                     "-codec:a", "libmp3lame", "-b:a", "64k", tmp.name],
                    capture_output=True))
                if proc.returncode == 0 and os.path.getsize(tmp.name) > 0:
                    send_path = tmp.name
                    tmp_converted = tmp.name
                else:
                    log.warning("[talk] ffmpeg convert failed — sending as-is")

            fname = os.path.basename(send_path) or "voice.mp3"

            # 1) probe Draft folder
            code, r = await asyncio.to_thread(lambda: self._ocs_api_raw(
                f"{API_V1}/chat/{chat_id}/attachment/folder", {"fileNames": [fname]}))
            if code != 200:
                raise RuntimeError(f"probe folder {code}")
            folder = r["ocs"]["data"]["folder"]

            # 2) upload to Draft under a uuid (стриминг из файла, без RAM-копии)
            tmp_name = f"{uuid.uuid4().hex}{os.path.splitext(send_path)[1]}"
            await asyncio.to_thread(lambda: self._webdav_put_file(
                f"/{folder}/{tmp_name}", send_path, "audio/mpeg"))

            # 3) attachment with the voice-message label
            meta: Dict[str, Any] = {"messageType": "voice-message"}
            if caption:
                meta["caption"] = caption[:1000]
            code, r = await asyncio.to_thread(lambda: self._ocs_api_raw(
                f"{API_V1}/chat/{chat_id}/attachment", {
                    "filePath": f"/{folder}/{tmp_name}",
                    "referenceId": uuid.uuid4().hex,
                    "talkMetaData": json.dumps(meta),
                    "fileName": fname,
                }))
            if code == 200:
                return SendResult(success=True)
            log.warning("[talk] attachment %s — falling back to share-as-file", code)
            return await self.send_file(chat_id, audio_path, caption=caption)
        except Exception as e:
            log.warning("[talk] send_voice error: %s — falling back to share-as-file", e)
            return await self.send_file(chat_id, audio_path, caption=caption)
        finally:
            if tmp_converted:
                try:
                    os.unlink(tmp_converted)
                except OSError:
                    pass

    def _ocs_api_raw(self, path: str, payload: Dict):
        """Sync OCS POST → (status, parsed json | raw str)."""
        import base64
        cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req = urllib.request.Request(
            self._url(path),
            data=json.dumps(payload).encode(),
            headers={"OCS-APIRequest": "true", "Accept": "application/json",
                     "Content-Type": "application/json",
                     "Authorization": f"Basic {cred}"})
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:300]

    @staticmethod
    def _guess_ctype(path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        return {".ogg": "audio/ogg", ".oga": "audio/ogg", ".mp3": "audio/mpeg",
                ".wav": "audio/wav", ".opus": "audio/opus",
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif",
                ".pdf": "application/pdf"}.get(ext, "application/octet-stream")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        room = await self._get(f"{API_V4}/room/{chat_id}")
        return {"name": room.get("displayName", chat_id), "type": "dm"}


# ---------- Plugin registration ----------

def check_requirements() -> bool:
    return HTTPX_AVAILABLE


def validate_config(cfg: PlatformConfig) -> bool:
    extra = getattr(cfg, "extra", None) or {}
    return bool(_env("TALK_SERVER_URL") or extra.get("server"))


def is_connected(cfg: PlatformConfig) -> bool:
    return False


def _env_enablement() -> Optional[dict]:
    if _env("TALK_SERVER_URL") and _env("TALK_APP_PASSWORD"):
        extra = {
            "server": _env("TALK_SERVER_URL"),
            "user": _env("TALK_USER"),
            "app_password": _env("TALK_APP_PASSWORD"),
            "room_token": _env("TALK_ROOM_TOKEN"),
        }
        home = {"token": _env("TALK_HOME_CHANNEL")} if _env("TALK_HOME_CHANNEL") else None
        return {"extra": extra, "home_channel": home}
    return None


def _standalone_send(chat_id: str, content: str, **kwargs) -> Dict:
    import base64
    server = _env("TALK_SERVER_URL").rstrip("/")
    user = _env("TALK_USER")
    pwd = _env("TALK_APP_PASSWORD")
    cred = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    r = urllib.request.Request(
        f"{server}{API_V1}/chat/{chat_id}",
        data=json.dumps({"message": content[:MAX_MESSAGE_LENGTH]}).encode(),
        headers={"OCS-APIRequest": "true", "Accept": "application/json",
                 "Content-Type": "application/json",
                 "Authorization": f"Basic {cred}"})
    resp = urllib.request.urlopen(r, timeout=30)
    return {"success": 200 <= resp.status < 300}


def register(ctx) -> None:
    ctx.register_platform(
        name="talk",
        label="Nextcloud Talk",
        adapter_factory=lambda cfg: NextcloudTalkAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["TALK_SERVER_URL", "TALK_USER", "TALK_APP_PASSWORD"],
        install_hint="pip install httpx   # already a Hermes dependency",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="TALK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        allow_update_command=True,
        platform_hint=(
            "You are communicating via Nextcloud Talk (room token = chat_id). "
            "Keep responses concise. Voice replies are delivered as audio attachments."
        ),
    )
