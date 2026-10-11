"""HPB (standalone signaling) client for the Nextcloud Talk adapter.

Provides: instant events (instead of 30s long-poll) and the typing indicator.

Flow (verified live 2026-09-28):
  1. OCS POST /room/{token}/participants/active  -> NC sessionId (255 bytes)
  2. WSS {hpb_url} → welcome → hello v1 (auth.url + {userid, ticket})
  3. WS  {"type":"room","room":{"roomid":token,"sessionid":NC_SESSION_ID}}
     <- room joined. IMPORTANT: without sessionid in the room request NC replies
     'no_such_room / not invited' (backendRoom looks up the participant by session).
  4. typing: {"type":"message","message":{"roomid":token,
     "data":"{\"type\":\"typing\",\"stream\":{\"signal\":\"typing\"}"}"}}
  5. Inbound chat events: {"type":"event","event":{"target":"room",...}}

On any HPB failure the adapter keeps working via long-poll.
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

# Typing ping must repeat while 'typing' (indicator expires after ~5s)
TYPING_INTERVAL = 4.0
RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0


class HPBClient:
    """Standalone-signaling WS client for a single Talk room."""

    def __init__(self, server: str, user: str, password: str,
                 room_token: str, hpb_url: str,
                 on_event=None):
        self.server = server.rstrip("/")
        self.user = user
        self.password = password
        self.room = room_token
        self.hpb_url = hpb_url
        self.on_event = on_event          # async callable(dict) — HPB events
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
        self._participants: set = set()  # signaling sessionids of room participants

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
        """participants/active -> creates the NC session, returns sessionid."""
        data = await self._ocs_post(
            client, f"{API_V4}/room/{self.room}/participants/active", {})
        sid = data.get("sessionId")
        if not sid:
            raise RuntimeError("participants/active returned an empty sessionId")
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
            raise RuntimeError("no helloAuthParams 1.0 in signaling settings")
        wss = st["server"].replace("https://", "wss://").replace(
            "http://", "ws://").rstrip("/") + "/spreed"

        # 2) NC session (required for room-join!)
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
            # outgoing typing handler lives in a separate task
            self._typing_task = asyncio.create_task(self._typing_loop(ws))

            # 3) read events
            async for raw in ws:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._track_participants(ev)
                if ev.get("type") == "event" and self.on_event:
                    try:
                        await self.on_event(ev.get("event", {}))
                    except Exception as e:
                        log.warning("[hpb] on_event error: %s", e)

    # ---------- typing ----------

    def _track_participants(self, ev: dict) -> None:
        """Track signaling sessionids of room participants (join/leave + room reply)."""
        try:
            if ev.get("type") == "room":
                parts = (ev.get("room", {}) or {}).get("participants") or []
                self._participants = {p.get("sessionId") or p.get("sessionid")
                                      for p in parts if p.get("sessionId") or p.get("sessionid")}
                return
            if ev.get("type") != "event":
                return
            e = ev.get("event", {}) or {}
            if e.get("type") == "join":
                for u in e.get("join") or []:
                    sid = u.get("sessionid")
                    if sid:
                        self._participants.add(sid)
            elif e.get("type") == "leave":
                for sid in e.get("leave") or []:
                    self._participants.discard(sid)
        except Exception as e:
            log.debug("[hpb] track participants: %s", e)

    def set_typing(self, active: bool) -> None:
        """Enable/disable the typing indicator (driven by the adapter)."""
        self._typing_until = float("inf") if active else 0.0

    async def _typing_loop(self, ws) -> None:
        """Peer-to-peer typing per the Talk client protocol (SignalingTypingHandler):
        send to every room participant except ourselves, personally —
        {type: startedTyping|stoppedTyping}; the server fills in "from".
        The legacy {"type":"typing","stream":...} format is ignored by
        current Talk clients."""
        announced = False  # typing already announced to the current roster
        while True:
            typing_active = time.time() < self._typing_until
            if typing_active and self._participants:
                for sid in self._participants:
                    if sid == self._public_sid:
                        continue
                    await ws.send(json.dumps({
                        "type": "message",
                        "message": {
                            "recipient": {"type": "session", "sessionid": sid},
                            "data": {"type": "startedTyping"}}}))
                announced = True
            elif announced:
                for sid in self._participants:
                    if sid == self._public_sid:
                        continue
                    try:
                        await ws.send(json.dumps({
                            "type": "message",
                            "message": {
                                "recipient": {"type": "session", "sessionid": sid},
                                "data": {"type": "stoppedTyping"}}}))
                    except Exception:
                        pass
                announced = False
            await asyncio.sleep(TYPING_INTERVAL)
