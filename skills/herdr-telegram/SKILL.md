---
name: herdr-telegram
description: "Send Telegram messages, screenshots, status sweeps, and Tailscale mobile dev-server preview links from a Herdr agent pane, inspect inbound Telegram photos/voice prompts, and configure or extend herdr-telegram. Use when the user asks to text/notify them on Telegram, send a screenshot, share a mobile preview URL, inspect an inbound Telegram image, or set up/customize herdr-telegram."
---

# herdr-telegram

Use `telegram-send` and `ts-preview` (or `~/.local/bin/telegram-send` and `~/.local/bin/ts-preview` if `~/.local/bin` is not on `PATH`) to message the user on Telegram or expose local dev servers to their phone over Tailscale.

## Automatic vs. Proactive Notifications

- The background `herdr-telegram` daemon (`systemd --user` on Linux, `launchd` on macOS) automatically notifies the user when a Herdr agent transitions to `done`/`idle` or pauses on a `blocked` prompt, and routes swipe-replies back into `$HERDR_PANE_ID`.
- Call `telegram-send` directly only when the user asks you to text or notify them, when sending a screenshot (`--photo`), or when reporting a scheduled/unattended sweep.

## Sending Messages & Screenshots (`telegram-send`)

Never read or print `~/.config/herdr-telegram/env`. `telegram-send` loads credentials internally without exposing tokens and binds the user's swipe-reply back to `$HERDR_PANE_ID`:

```bash
# Send a message (auto-detects $HERDR_PANE_ID and routes via config.toml [[routes]])
telegram-send "Database migration passed; ready for smoke test?"

# Send a UI screenshot, chart, or visual diff (PNG, JPEG, WebP, or GIF)
telegram-send --photo ./artifacts/mobile-check.png "Updated mobile layout; approve?"

# Target a specific named bot channel from config.toml (such as --bot alerts)
telegram-send --bot alerts "🟢 All 12 HTTP health checks passing"
```

### Status & Sweep Formatting

In status, health-check, or support messages sent via `telegram-send`:
- Give each passed check its own `🟢` line.
- Prefix findings with `🔴 [P1]` (urgent/broken), `🟡 [P2]` (warning/non-blocking issue), or `📝 [Note]` (neutral FYI).
- Avoid screenshotting raw secrets or credentials (`--photo` pixels cannot be text-redacted).

## Inspecting Inbound Photos from Telegram

When the user sends a photo, multi-photo album, or image file from Telegram:
- `herdr-telegram` verifies the image bytes, saves the file(s) atomically (`0600`) inside `<workspace>/.herdr-telegram/media/` (auto-excluded from `git status`), and prompts your pane with the exact local file path(s).
- Open the provided `.herdr-telegram/media/...` path(s) directly with your image/file reader tool and address the user's caption.

## Mobile Dev-Server Previews (`ts-preview`)

To expose a local dev server (`localhost:3000`, `5173`, `8080`, etc.) to the user's phone over their Tailscale tailnet and push a clickable link to Telegram:

```bash
# Expose localhost:5173 over Tailscale Serve and text the URL to Telegram
ts-preview 5173 --telegram

# Expose a specific path with a custom label
ts-preview http://localhost:8080/docs/ --label "API Docs" --telegram

# Inspect active routes or check Tailscale Serve readiness
ts-preview status
ts-preview doctor

# Tear down a specific port when finished (do not run 'ts-preview reset' unless asked)
ts-preview off 5173
```

## Configuring or Modifying `herdr-telegram`

When the user asks you to set up, diagnose, toggle optional features (such as turning `voice_provider` or `summary_provider` on/off, adding `[bots.<name>]`, `[[routes]]`, or `[agents.<alias>]`), or modify `herdr-telegram` itself:

- Check health safely with `herdr-telegram doctor --offline` (or `--json`); never print raw tokens from `~/.config/herdr-telegram/env`.
- Edit `~/.config/herdr-telegram/config.toml` (see `config.example.toml` in `~/.config/herdr-telegram/` or `~/.local/share/herdr-telegram/`) to configure routes, bots, custom agents, `summary_provider = "off"`, or `voice_provider = "off"`, then restart the service (`systemctl --user restart herdr-telegram.service` on Linux or `launchctl kickstart -k "gui/$(id -u)/io.github.jzhao234.herdr-telegram"` on macOS).
- Run `herdr-telegram setup-voice` only when the user wants local offline `faster-whisper` voice note transcription.
- Read `AGENTS.md` inside the `herdr-telegram` install/repo directory (`~/.local/share/herdr-telegram/AGENTS.md`) for the full module map, configuration reference, extension recipes, and test suite commands.
