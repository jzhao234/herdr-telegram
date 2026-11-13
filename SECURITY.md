# Security Policy

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `0.2.x` | Yes |
| `< 0.2.0` | No |

## Reporting a Vulnerability

Because `herdr-telegram` bridges Telegram chats to local terminal coding agents, please **do not report security vulnerabilities in public GitHub issues**.

Report suspected vulnerabilities privately through [GitHub Private Vulnerability Reporting](https://github.com/jzhao234/herdr-telegram/security/advisories/new). Include reproduction steps, affected files or commands, and the impact boundary. You can expect an initial response within 72 hours.

## Security Model & Trust Boundaries

- **Workstation trust boundary:** Your paired numeric `TELEGRAM_CHAT_ID` has authority to prompt, approve, and `/spawn` coding agents inside configured `spawn_roots`. Enable Telegram Two-Step Verification on your account and keep `~/.config/herdr-telegram/env` at mode `0600`.
- **Private 1:1 chat enforcement:** Group, supergroup, and channel updates are ignored. Interactive onboarding (`herdr-telegram setup`) requires a cryptographic one-time `/start <code>` token before writing `TELEGRAM_CHAT_ID`.
- **Callback TOCTOU protection:** Inline approval buttons bind `state_change_seq`, a `terminal_id` tag, and a dialog digest, re-checking live Herdr state before sending raw keys.
- **Secret isolation & redaction:** Outbound digests and messages redact common API keys, tokens, private keys, JWTs, and Bearer headers; `HerdrClient` and `TurnSummarizer` scrub bridge secrets from child subprocess environments; remote HTTP endpoints enforce `https://` (`http://` allowed strictly on loopback) and block all HTTP redirects.
- **Media & preview boundaries:** Inbound and outbound images validate magic bytes (`JPEG`, `PNG`, `WebP`, `GIF`), refuse symlinks (`O_NOFOLLOW`), and stay isolated inside `<workspace>/.herdr-telegram/media/` (`0600`). Note that pixels inside screenshots sent via `telegram-send --photo` cannot be text-redacted. `ts-preview` only exposes loopback targets (`127.0.0.1`, `localhost`, `::1`) over your private Tailscale tailnet.
