"""Nextcloud Talk platform adapter (Hermes plugin)."""

import asyncio
import json
import logging
import os
import re
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

# Confirmation-by-reaction (кнопки-подтверждения, как inline в Telegram):
#   ✅  = подтвердить            (approve)
#   ❌  = отклонить              (decline)
#   ⏩  = «дальше без спроса» — запомнить выбор и применять к таким вопросам в этой сессии
CONFIRM_EMOJI_APPROVE = "✅"
CONFIRM_EMOJI_DECLINE = "❌"
CONFIRM_EMOJI_SESSION = "⏩"
CONFIRM_EMOJIS = (CONFIRM_EMOJI_APPROVE, CONFIRM_EMOJI_DECLINE, CONFIRM_EMOJI_SESSION)
CONFIRM_TIMEOUT = 120.0        # сек на реакцию


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
        # ---- confirmation-by-reaction ----
        self._pending_confirms: Dict[str, Dict] = {}   # key "room:msgid" -> {fut, room, msgid, actor}
        self._session_scope: Dict[str, set] = {}       # scope-name -> set('approve'|'decline') («✔️ в сессию»)
        self._confirm_watcher: Optional[asyncio.Task] = None

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

    async def _get_poll(self, path: str, params: Optional[Dict] = None) -> Optional[Any]:
        """Long-poll GET: 304 (Not Modified) — нормальный пустой цикл, не ошибка.

        Возвращает None при 304, иначе ocs.data. Экономит время: без
        exception-handling на каждом 30-секундном холостом цикле."""
        r = await self._client.get(self._url(path), params=params, headers=self._headers())
        if r.status_code == 304:
            return None
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
        # резолвим все висящие confirm-фьючерсы отменой
        for pend in self._pending_confirms.values():
            if not pend["fut"].done():
                pend["fut"].cancel()
        self._pending_confirms.clear()
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
                data = await self._get_poll(f"{API_V1}/chat/{token}", params)
                streak = 0
                msgs = data if isinstance(data, list) else []
                for m in msgs:
                    mid = m.get("id", 0)
                    self._last_msg_ids[token] = max(self._last_msg_ids.get(token, 0), mid)
                    # ---- system-сообщение «reaction»: <emoji> — это клик по кнопке ----
                    if (m.get("messageType") == "system" and m.get("actorId") != self.user
                            and (m.get("message") or "") in CONFIRM_EMOJIS
                            and self._pending_confirms):
                        await self._resolve_confirm_from_system(token, m)
                        continue
                    if m.get("actorId") == self.user:
                        continue
                    processed = self._processed_ids.setdefault(token, set())
                    if mid in processed:
                        continue
                    processed.add(mid)
                    if len(processed) > 500:
                        self._processed_ids[token] = set(sorted(processed)[-200:])
                    mtype = MessageType.VOICE if m.get("messageType") == "voice-message" else MessageType.TEXT
                    if m.get("messageType") == "file":
                        # не аудио и не картинка → DOCUMENT, иначе точный тип
                        mime = ((m.get("messageParameters") or {}).get("file") or {}).get("mimetype", "")
                        if mime.startswith("image/"):
                            mtype = MessageType.PHOTO
                        elif mime.startswith("video/"):
                            mtype = MessageType.VIDEO
                        elif mime.startswith("audio/"):
                            mtype = MessageType.AUDIO
                        else:
                            mtype = MessageType.DOCUMENT
                    actor = m.get("actorId") or "unknown"
                    # ---- реакции как кнопки подтверждения ----
                    reactions = m.get("reactions") or {}
                    if reactions and self._pending_confirms:
                        await self._check_confirm_reaction(token, mid, reactions)
                    # ---- вложение (голосовое/файл): скачать локально ----
                    file_info = (m.get("messageParameters") or {}).get("file") or {}
                    local_path = None
                    if file_info.get("path"):
                        local_path = await asyncio.to_thread(
                            self._download_attachment_sync, file_info["path"],
                            f"{token}_{mid}_{file_info.get('name', 'file')}")
                        if local_path is None:
                            log.warning("[talk] attachment download failed: %s",
                                        file_info.get("name"))
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
                        "file_path": local_path,
                        "file_name": file_info.get("name"),
                        "file_mimetype": file_info.get("mimetype"),
                    }
                    await self.handle_message(ev)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (304, 404):
                    # 304 уже обработан в _get_poll; 404 — комната исчезла: тихо
                    continue
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

    async def _resolve_confirm_from_system(self, room: str, m: Dict) -> None:
        """System-сообщение вида '<emoji>' (reaction от человека) → резолвим
        самый свежий pending-вопрос этой комнаты."""
        emoji = m.get("message")
        actor = m.get("actorId")
        # ищем pending-вопрос этой комнаты (при нескольких — ближайший по времени)
        cands = [(k, p) for k, p in self._pending_confirms.items()
                 if p["room"] == room]
        if not cands:
            return
        # реакция на конкретное сообщение ссылается на parent через...
        # system-reaction НЕ несёт id цели — считаем, что отвечают на последний вопрос
        key, pend = max(cands, key=lambda kv: kv[1]["msgid"])
        self._pending_confirms.pop(key, None)
        if pend["fut"].done():
            return
        if emoji == CONFIRM_EMOJI_APPROVE:
            pend["fut"].set_result(("approve", False))
        elif emoji == CONFIRM_EMOJI_DECLINE:
            pend["fut"].set_result(("decline", False))
        else:  # ⏩ — да + запомнить выбор по scope
            pend["fut"].set_result(("approve", True))

    # ---------- Confirmation by reaction (кнопки ✅ / ❌ / ⏩) ----------

    def _check_session_scope(self, scope: Optional[str]) -> Optional[str]:
        """Если по этому scope уже есть выбор «⏩ без спроса» — вернуть его."""
        if scope:
            saved = self._session_scope.get(scope)
            if saved:
                return next(iter(saved))
        return None

    async def ask_confirm(self, chat_id: str, question: str, *,
                          scope: Optional[str] = None,
                          timeout: float = CONFIRM_TIMEOUT,
                          default: str = "decline") -> tuple:
        """Кнопки-подтверждение через реакции.

        Отправляет вопрос и ставит три реакции-«кнопки»: ✅ / ❌ / ⏩.
        Возвращает (decision, remember):
          decision ∈ 'approve' | 'decline', remember=True если жали ⏩.
        scope — именованная категория («delete_file», «send_email», ...):
          повторный вопрос того же scope при remember=True не задаётся.
        """
        remembered = self._check_session_scope(scope)
        if remembered:
            return (remembered, False)

        q = question if len(question) <= 3000 else question[:2997] + "…"
        text = (f"{q}\n\n✅ — да   ·   ❌ — нет   ·   ⏩ — да и дальше без спроса"
                f"   (⏱ {int(timeout)} с)")
        r = await self._post(f"{API_V1}/chat/{chat_id}", {"message": text})
        msg_id = r.get("id")
        for emoji in CONFIRM_EMOJIS:
            try:
                await self._post(f"{API_V1}/reaction/{chat_id}/{msg_id}",
                                 {"reaction": emoji})
            except Exception as e:
                log.warning("confirm react %s: %s", emoji, e)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_confirms[f"{chat_id}:{msg_id}"] = {
            "fut": fut, "room": chat_id, "msgid": msg_id,
            "default": default,
        }
        try:
            decision, remember = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_confirms.pop(f"{chat_id}:{msg_id}", None)
            decision, remember = (default, False)
            try:
                await self.send(chat_id, f"⏱ Не дождался реакции — отмена.")
            except Exception:
                pass
        if remember and scope:
            self._session_scope[scope] = {decision}
        return (decision, remember)

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

        try:
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
                await asyncio.sleep(0.5)
        finally:
            # остановка ЛЮБЫМ путём: _running=False, cancel listen, ошибка —
            # все room-таски отменяются гарантированно (никаких зависаний)
            for t in self._room_tasks.values():
                t.cancel()
            if self._room_tasks:
                await asyncio.gather(*self._room_tasks.values(), return_exceptions=True)
            self._room_tasks.clear()

    # Маркер для LLM-ответов: строка «[[CONFIRM:scope|Вопрос?]]» в тексте ответа
    # превращается в реакционные кнопки ✅/❌/⏩ (ask_confirm). Результат придёт
    # в следующее сообщение пользователя как «[[CONFIRMED:scope|approve|false]]».
    CONFIRM_MARKER_RE = re.compile(r"\[\[CONFIRM:([^|\]]+)\|([^\]]+)\]\]")

    async def send(self, chat_id: str, content: str,
                   reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Send text. metadata supports: thread_id (reply in thread),
        thread_title (create a new thread), silent.

        LLM integration: a line «[[CONFIRM:scope|Question?]]» inside content is
        replaced by the reaction-buttons flow (ask_confirm) — the question is
        sent, buttons attached, and the answer is awaited inline (blocking send,
        up to CONFIRM_TIMEOUT). The decision is appended to the chat as a
        «✅ Подтверждено / ❌ Отменено» notice."""
        m = self.CONFIRM_MARKER_RE.search(content or "")
        if m and not (metadata or {}).get("silent"):
            scope, question = m.group(1).strip(), m.group(2).strip()
            head = content[:m.start()].rstrip()
            tail = content[m.end():].lstrip()
            if head:
                await self._post(f"{API_V1}/chat/{chat_id}",
                                 {"message": head[:MAX_MESSAGE_LENGTH]})
            decision, remember = await self.ask_confirm(
                chat_id, question, scope=scope or None)
            verdict = ("✅ Подтверждено" if decision == "approve" else "❌ Отменено")
            if remember:
                verdict += " (и запомнено на сессию)"
            notice = f"{verdict}: {question}"
            if tail:
                notice += "\n\n" + tail
            await self._post(f"{API_V1}/chat/{chat_id}",
                             {"message": notice[:MAX_MESSAGE_LENGTH]})
            return SendResult(success=True)
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

    TALK_INCOMING_DIR = "/opt/data/cache/talk_files"   # куда складывать входящие вложения

    def _download_attachment_sync(self, remote_path: str, suggested_name: str) -> Optional[str]:
        """Скачать входящее вложение из Talk (WebDAV) локально.

        ВАЖНО (проверено на реальных записях Talk 23.09 и 27.09.2026): Talk
        пишет голосовые как **AAC в M4A-контейнере**, но сохраняет под именем
        *.mp3 и отдаёт Content-Type audio/mpeg. Расширению доверять нельзя —
        скачиваем как есть; декодеры (ffmpeg/faster-whisper) определяют формат
        по контенту сами.
        Возвращает локальный путь или None при ошибке.
        """
        import base64
        import urllib.parse
        try:
            os.makedirs(self.TALK_INCOMING_DIR, exist_ok=True)
            cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            url = (f"{self.server}/remote.php/dav/files/{self.user}/"
                   f"{urllib.parse.quote(remote_path.lstrip('/'))}")
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {cred}"})
            data = urllib.request.urlopen(req, timeout=120).read()
            # имя: token_msgid_name, но расширение берём из remote (не выдумываем)
            safe = re.sub(r"[^\w.\-]+", "_", suggested_name)[-120:]
            local = os.path.join(self.TALK_INCOMING_DIR, safe)
            with open(local, "wb") as fh:
                fh.write(data)
            log.info("[talk] attachment saved: %s (%d bytes)", local, len(data))
            return local
        except Exception as e:
            log.warning("[talk] download_attachment %s: %s", remote_path, e)
            return None

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
            "Keep responses concise. Voice replies are delivered as audio attachments. "
            "CONFIRMATIONS: for risky/irreversible actions embed a marker line "
            "[[CONFIRM:scope|Question?]] in your reply (scope = short category like "
            "delete_file). The adapter turns it into reaction-buttons ✅/❌/⏩ and "
            "blocks until the user reacts (or timeout). After the user taps ⏩ once, "
            "same-scope questions are auto-approved for the session. Do NOT ask "
            "confirmation in plain text yourself."
        ),
    )
