# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] — 2026-09-06

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
