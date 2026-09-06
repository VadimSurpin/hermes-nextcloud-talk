# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | ✅ |

## Configuration recommendations

1. **App password, not your main password.** Create a dedicated app password (Settings → Security) with minimal scope. If compromised, it can be revoked without changing the main password.

2. **Private server.** The adapter talks to your Nextcloud with full rights of the `hermes` user. Don't point `TALK_SERVER_URL` at public servers you don't control.

3. **Pairing is mandatory.** Do not bypass the pairing mechanism: without approval (`hermes pairing approve talk <CODE>`), all user messages are ignored.

4. **Room allowlist.** The adapter polls rooms from `room_token`/`home_channel`. Don't add public rooms — any participant would be able to trigger the agent.

5. **No secrets in git.** `app_password` and tokens are never stored in the repository; README examples use placeholders.

## Reporting a vulnerability

Found a security issue — **please do not open a public issue**. Contact the repository owner directly (see GitHub profile) with a description and reproduction steps. Expect a response within 72 hours.

## Known limitations

- The adapter performs actions as the `hermes` user: messages, files, and shares appear as its activity in the Nextcloud audit log.
- Attachment files are stored in `Files/Talk/<room>-subfolder` and are visible to all room participants.
