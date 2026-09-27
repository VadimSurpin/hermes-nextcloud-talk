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

## [Unreleased]

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
