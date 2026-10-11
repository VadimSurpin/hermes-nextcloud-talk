"""HPB (standalone signaling) client for the Nextcloud Talk adapter.

Даёт: мгновенные события (вместо 30-с long-poll) и typing-индикатор.

Флоу (проверено live 28.09.2026):
  1. OCS POST /room/{token}/participants/active  -> NC sessionId (255 байт)
  2. WSS {hpb_url} → welcome → hello v1 (auth.url + {userid, ticket})
  3. WS  {"type":"room","room":{"roomid":token,"sessionid":NC_SESSION_ID}}
     ← room joined. ВАЖНО: без sessionid в room-запросе NC отвечает
     «no_such_room / not invited» (backendRoom ищет participant по session).
  4. typing: {"type":"message","message":{"roomid":token,
     "data":"{\"type\":\"typing\",\"stream\":{\"signal\":\"typing\"}"}"}}
  5. Входящие события чата: {"type":"event","event":{"target":"room",...}}

При любом сбое HPB адаптер продолжает работать через long-poll.
"""

import asyncio
import json
import logging
import re
import ssl
import time

import httpx
import websockets

log = logging.getLogger(__name__)

API_V1 = "/ocs/v2.php/apps/spreed/api/v1"
API_V3 = "/ocs/v2.php/apps/spreed/api/v3"
API_V4 = "/ocs/v2.php/apps/spreed/api/v4"

# Typing-пинг надо повторять, пока «печатаем» (индикатор гаснет через ~5 c)
TYPING_INTERVAL = 4.0
RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0


class HPBClient:
    """WS-клиент standalone-signaling для одной комнаты Talk."""

    def __init__(self, server: str, user: str, password: str,
                 room_token: str, hpb_url: str,
                 on_event=None):
        self.server = server.rstrip("/")
        self.user = user
        self.password = password
        self.room = room_token
        self.hpb_url = hpb_url
        self.on_event = on_event          # async callable(dict) — события HPB
        self._headers = {
            "OCS-APIRequest": "true",
            "Accept": "application/json",
            "Authorization": "Basic " + __import__("base64").b64encode(
                f"{user}:{password}".encode()).decode(),
        }
        self._ticket = None
        self._userid = None
        self._task: asyncio.Task | None = None
        self._typing_task: asyncio.Task | None = None
        self._typing_until = 0.0
        self._connected = False
        self._public_sid = None

    # ---------- OCS helpers ----------

    async def _ocs_post(self, client: httpx.AsyncClient, path: str,
                        payload: dict) -> dict:
        r = await client.post(f"{self.server}{path}", json=payload,
                              headers=self._headers)
        r.raise_for_status()
        return r.json().get("ocs", {}).get("data", {})

    async def _fetch_settings(self, client: httpx.AsyncClient) -> dict:
        """signaling settings: server URL + helloAuthParams (userid/ticket)."""
        r = await client.get(
            f"{self.server}{API_V3}/signaling/settings",
            params={"token": self.room}, headers=self._headers)
        r.raise_for_status()
        return r.json().get("ocs", {}).get("data", {})

    async def _ocs_join_room(self, client: httpx.AsyncClient) -> str:
        """participants/active → создаёт NC-session, возвращает sessionid."""
        data = await self._ocs_post(
            client, f"{API_V4}/room/{self.room}/participants/active", {})
        sid = data.get("sessionId")
        if not sid:
            raise RuntimeError("participants/active вернул пустой sessionId")
        return sid

    # ---------- main loop ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._typing_task:
            self._typing_task.cancel()

    async def _run(self) -> None:
        backoff = RECONNECT_MIN
        async with httpx.AsyncClient(timeout=30) as http:
            while True:
                try:
                    await self._session_loop(http)
                    backoff = RECONNECT_MIN
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("[hpb] session loop failed: %s", e)
                self._connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    async def _session_loop(self, http: httpx.AsyncClient) -> None:
        # 1) signaling settings: HPB URL + ticket
        st = await self._fetch_settings(http)
        hello_params = (st.get("helloAuthParams") or {}).get("1.0")
        if not hello_params:
            raise RuntimeError("нет helloAuthParams 1.0 в signaling settings")
        wss = st["server"].replace("https://", "wss://").replace(
            "http://", "ws://").rstrip("/") + "/spreed"

        # 2) NC session (обязателен для room-join!)
        nc_session = await self._ocs_join_room(http)

        ssl_ctx = ssl.create_default_context()
        async with websockets.connect(wss, ssl=ssl_ctx) as ws:
            await ws.recv()  # welcome
            await ws.send(json.dumps({
                "type": "hello", "hello": {
                    "version": "1.0",
                    "auth": {"url": f"{self.server}{API_V1}/signaling/welcome",
                             "params": hello_params}}}))
            hello = json.loads(await ws.recv())
            if hello.get("type") != "hello":
                raise RuntimeError(f"hello failed: {json.dumps(hello)[:200]}")
            self._public_sid = hello["hello"]["sessionid"]

            await ws.send(json.dumps({
                "type": "room",
                "room": {"roomid": self.room, "sessionid": nc_session}}))
            joined = json.loads(await ws.recv())
            if joined.get("type") != "room":
                raise RuntimeError(f"room join failed: {json.dumps(joined)[:200]}")

            self._connected = True
            log.info("[hpb] joined room %s (instant events ON)", self.room)

            # handler для исходящего typing живёт в отдельной задаче
            self._typing_task = asyncio.create_task(self._typing_loop(ws))

            # 3) читаем события
            async for raw in ws:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "event" and self.on_event:
                    try:
                        await self.on_event(ev.get("event", {}))
                    except Exception as e:
                        log.warning("[hpb] on_event error: %s", e)

    # ---------- typing ----------

    def set_typing(self, active: bool) -> None:
        """Включить/выключить индикатор «печатает» (управляется адаптером)."""
        self._typing_until = float("inf") if active else 0.0

    async def _typing_loop(self, ws) -> None:
        while True:
            if time.time() < self._typing_until and self._public_sid:
                await ws.send(json.dumps({
                    "type": "message",
                    "message": {
                        "recipient": {"type": "room",
                                      "sessionid": self._public_sid},
                        "data": {"type": "typing",
                                 "stream": {"signal": "typing"}}}}))
            await asyncio.sleep(TYPING_INTERVAL)
