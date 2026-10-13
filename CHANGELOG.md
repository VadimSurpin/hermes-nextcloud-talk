# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.0] — 2026-09-23

### Added
- Reactions: `react()`, `unreact()`, `get_reactions()` via `/reaction/{token}/{messageId}`.
- Threads: `send(metadata={"thread_title": ...})` creates a thread; `{"thread_id": ...}` replies into one.
- `listen()` now maps `threadId`, `reactions`, and `replyTo` into `MessageEvent` (`reply_to_message_id` + `metadata`).
- `send()` supports `silent` flag.
- **Reaction confirmations** `ask_confirm(chat_id, question, scope, timeout, default)`: emulates
  Telegram-style confirm buttons with reactions ✅ / ❌ / ⏩ ("yes & stop asking" for a named scope).
  Human reactions arrive in the long-poll as system messages `<emoji>` and are matched against
  pending questions (`_resolve_confirm_from_system`); timeout → `default` + cancellation notice.
- **Incoming attachment download**: voice notes and files are auto-downloaded to
  `/opt/data/cache/talk_files/` via WebDAV and exposed as `metadata.file_path` / `file_name` /
  `file_mimetype` on the `MessageEvent`. File messages are typed by MIME
  (PHOTO/VIDEO/AUDIO/DOCUMENT).

### Fixed
- Attachment download URL encoding: Talk recording names contain spaces/parens — path is now
  percent-encoded per segment (404 → 200).
- Voice-note format reality: Talk records **AAC in an M4A container named `.mp3`**
  (`Content-Type: audio/mpeg`) — documented; decode by content, never by extension.

## [1.5.0] — 2026-10-13

### Fixed
- **Typing indicator: room broadcast instead of per-session sends.** Current
  Talk clients (`SignalingTypingHandler`) only react to peer-to-peer
  `{type: "startedTyping" | "stoppedTyping"}` messages; the legacy
  `{"type":"typing","stream":...}` broadcast is silently ignored. Sending
  `recipient: {type: "room"}` (server relays to everyone except the sender)
  and repeating every ~4 s — per-session participant tracking is gone, app
  reconnects (phone sleep, network switch) can no longer orphan the indicator.
- **Typing task survives WS reconnects.** The typing loop held a reference to
  one WS object; when the signaling socket dropped (server keepalive, network
  blip), the reconnect created a new socket but the old loop kept sending into
  the dead one — the indicator silently died until the next adapter restart.
  The previous task is now cancelled on reconnect, and the loop exits on send
  failure instead of retrying a dead socket.
- **Confirm buttons: instant resolve via reaction poller.** Clicks used to be
  picked up by the 30 s long-poll, which read as «кнопки не работают». While a
  question is pending, a 2 s poller queries
  `GET /reaction/{token}/{messageId}` directly (format trap: it returns a
  dict `{emoji: [{actorId,...}]}`, and helper wrappers already unwrap
  `ocs.data` — double-unwrapping yielded `{}` every poll and the fast path
  silently never fired). Verified live: click → verdict in 2–3 s.
- **Typing turns on the instant a button is pressed** — in every
  confirm-resolve path (system message, reactions summary, poller), before
  the verdict lands.
- **Outgoing text with `{word}` sequences no longer fails with HTTP 400.**
  Talk parses `{actor}`-style sequences as rich-object placeholders and
  rejects unknown ones (plus the plain-text fallback also failed). Literal
  braces are now isolated with invisible characters (U+2066/U+2069) — renders
  unchanged for the user.
- **Typing on the HPB event path**: `start_typing()` was only called on the
  legacy long-poll path, so messages received via instant HPB events never
  triggered the indicator. Now invoked on both paths.

## [1.3.0] — 2026-09-28

### Added
- **HPB (standalone signaling) support** (`hpb.py`): optional WS client giving
  instant room events and the native «typing…» indicator.
  - Key discovery: the WS room-join **must carry the Nextcloud sessionId** obtained
    from `POST /room/{token}/participants/active` — without it NC's backend
    room-check rejects the bot with `no_such_room / not invited` (participant is
    looked up by session first, and the userId fallback is never reached).
  - Typing: `{"type":"message","message":{"roomid":…,"data":"{\"type\":\"typing\",
    \"stream\":{\"signal\":\"typing\"}}"}}` — `data` must be a **JSON string**,
    refreshed every ~4 s; typing is switched on when a message arrives and off in `send()`.
  - Any HPB failure logs a warning and the adapter silently falls back to long-poll.
- Requires `websockets` + `httpx` in the gateway venv (both already present).

## [1.2.0] — 2026-09-27

### Added
- **LLM-facing confirmations**: embed `[[CONFIRM:scope|Question?]]` in a reply —
  `send()` intercepts the marker, posts the question with ✅/❌/⏩ reaction buttons
  (via `ask_confirm`), blocks until the user reacts or timeout, then posts a
  verdict message (✅ Подтверждено / ❌ Отменено). After ⏩ the scope is remembered
  for the session and further same-scope markers are auto-approved without buttons.
- `platform_hint` now documents the marker for the LLM.
- README: reaction-confirmations section updated with the marker syntax.

## [1.1.1] — 2026-09-27

### Fixed
- **Latency**: HTTP 304 (empty long-poll cycle) no longer raised as an exception —
  handled in `_get_poll()` and treated as a normal empty loop. Removes spurious
  exponential backoff (up to 300 s) that could stall message delivery.
- Room-list refresh loop: 2 s → 0.5 s sleep — new rooms are picked up faster.

### Added
- Nextcloud Talk platform adapter for Hermes Agent (plugin API, `register(ctx)`).
- Message receiving: long-poll `GET /chat/{token}?lookIntoFuture=1` with own-message filtering by `actorId`.
- Text sending: `POST /chat/{token}`.
- Native voice messages: probe Draft-folder → WebDAV upload → attachment endpoint with `messageType: voice-message`; automatic OGG → MP3 conversion (ffmpeg, 64k) — Talk only accepts the label for `audio/mpeg`/`audio/wav`.
- Media and files (images/videos/documents): attachment endpoint with `caption` in `talkMetaData` — caption on the media itself.
- Delivery fallback: WebDAV to `Files/Talk` + share-to-chat (`shareType=10`).
- Cron delivery: `deliver=talk`, `TALK_HOME_CHANNEL` variable, standalone-sender for out-of-process cron.
- Env-enablement: auto-configuration from `TALK_*` variables before adapter construction.
- Pairing integration (`hermes pairing approve talk <CODE>`).
- Documentation: README with installation, protocol, debugging; common issues guide.
