# herdr-telegram

[![CI](https://github.com/jzhao234/herdr-telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/jzhao234/herdr-telegram/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](pyproject.toml)
[![Platform: Linux | macOS](https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey.svg)](install.sh)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](pyproject.toml)

Modular two-way Telegram bridge and Tailscale mobile preview toolkit for [Herdr](https://github.com/herdrdev/herdr) terminal coding agents (`claude`, `codex`, `gemini`, `agy`, `opencode`, `cursor`, `pi`, `amp`, `droid`, and custom agent profiles).

Runs as a lightweight user daemon (`systemd --user` on Linux, `launchd` on macOS) using pure Python 3.11+ standard library (`tomllib` included, zero pip dependencies). When an agent finishes unattended or pauses on an approval dialog, `herdr-telegram` sends a clean mobile digest or tap-ready option buttons to your phone, and routes your swipe-replies or screenshots right back into that terminal pane.

---

## What It Looks Like on Your Phone

<table>
<tr>
<th width="50%">Completed Turn Digest</th>
<th width="50%">Interactive Blocked Prompt</th>
</tr>
<tr>
<td>

```text
✅ Done · Auth (🤖 Opus)

• Admin email 2FA (auth#148 by 🤖): I added email
  second-factor support for admin step-up actions
  and blocked self-reset at the store layer,
  currently undergoing checks before merging.

🟢 gofmt, go vet & build
🟢 auth#148 lint & race tests (42 passed)

📝 [Note] Emergency CLI password reset remains
available to server operators over SSH.
```

</td>
<td>

```text
⚠️ Blocked · Auth (🤖 Opus)

Reset route

When an admin resets the password of another admin
who uses email codes for 2FA, how should the new
password reach them?

1. Temp password only (Recommended)
   Console offers a temporary password shared over
   a separate channel so both factors stay split.
2. Allow the email link
   Same as standard users; a compromised mailbox
   receives both factors after a reset.
3. Link + clear their 2FA
   Sends the reset link and forces 2FA re-setup at
   next sign-in.

[ 1. Temp password only (Recommended) ]
[ 2. Allow the email link             ]
[ 3. Link + clear their 2FA           ]
[ 💬 5. Chat about this               ]
[ 📖 Read output                      ]
```

</td>
</tr>
</table>

Swipe-reply to any card (or prefix `opus: ship it`) and `herdr-telegram` delivers your prompt straight into that pane, waiting for Herdr to confirm the lifecycle transition so dropped or stalled prompts never fail silently.

---

## One-Line Install (Linux & macOS)

Requires Linux (`systemd --user`) or macOS (`launchd`), `python3` (>= 3.11), `git`, and [Herdr](https://github.com/herdrdev/herdr).

```bash
curl -fsSL https://raw.githubusercontent.com/jzhao234/herdr-telegram/main/install.sh | bash
```

Or from a local clone (symlinks your checkout directly for easy hacking):

```bash
git clone https://github.com/jzhao234/herdr-telegram.git
cd herdr-telegram && ./install.sh
```

### 30-Second Interactive Pairing (`herdr-telegram setup`)

1. Message [`@BotFather`](https://t.me/BotFather) on Telegram with `/newbot` and copy your bot token.
2. Run the interactive onboarding wizard:
   ```bash
   herdr-telegram setup
   ```
   It prompts for your `@BotFather` token (hidden input), registers your bot's `/` command menu automatically, prints a one-time `https://t.me/<bot>?start=<code>` pairing link for your phone to capture your `TELEGRAM_CHAT_ID` securely, writes `~/.config/herdr-telegram/env` (`0600`), and starts the background service (`systemd --user` on Linux or `launchd` on macOS).
3. Run a full health check anytime:
   ```bash
   herdr-telegram doctor
   ```

<details>
<summary><b>Headless / manual setup, logs, CLI-only install (uv / pipx), and uninstall</b></summary>

- **Manual headless setup:** Add `TELEGRAM_BOT_TOKEN=<token>` to `~/.config/herdr-telegram/env`, start the service (`systemctl --user enable --now herdr-telegram.service` on Linux or `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.jzhao234.herdr-telegram.plist` on macOS), send `/start` to your bot to discover `TELEGRAM_CHAT_ID`, add it to `env`, and restart.
- **Inspecting service logs:** Run `journalctl --user -u herdr-telegram.service -f` on Linux or `tail -f ~/.local/state/herdr-telegram/bridge.log` on macOS.
- **Re-running updates:** Re-run the one-line install command anytime to pull updates cleanly without overwriting `~/.config/herdr-telegram/env` or `~/.config/herdr-telegram/config.toml`.
- **Uninstalling:** Run `~/.local/share/herdr-telegram/install.sh --uninstall` (or `curl -fsSL https://raw.githubusercontent.com/jzhao234/herdr-telegram/main/install.sh | bash -s -- --uninstall`) to stop the service and remove installed files while keeping `~/.config/herdr-telegram` (`--purge` removes bridge config and state files too).
- **Bundled coding-agent skill (`SKILL.md`):** Install [skills/herdr-telegram/SKILL.md](skills/herdr-telegram/SKILL.md) with `npx skills add jzhao234/herdr-telegram` (or `./install.sh --install-skill`) so Claude Code, Codex, and Gemini agents automatically know how to run `telegram-send`, attach `--photo` screenshots, read inbound `.herdr-telegram/media/` images, and share `ts-preview <port> --telegram` links.
- **Installing via `uv tool` or `pipx`:** `uv tool install git+https://github.com/jzhao234/herdr-telegram.git` installs the `herdr-telegram`, `telegram-send`, and `ts-preview` CLIs without registering an OS service (`install.sh` is recommended when you want automatic `systemd` / `launchd` service setup). Always install from this GitHub repository URL rather than bare `pip install telegram-send`, which is an unrelated legacy PyPI package.

</details>

---

## Modular Rules & Pluggable Agents (`config.toml`)

Out of the box, `~/.config/herdr-telegram/env` is all you need for a single-bot (or two-bot `TELEGRAM_PROD_BOT_TOKEN`) setup. When you want custom agent profiles, multiple Telegram bots, or per-workflow rules (for example an instant-delivery `ops` channel with silent all-green sweeps alongside a `90s`-debounced `[defaults]` coding channel), copy [config.example.toml](config.example.toml) to `~/.config/herdr-telegram/config.toml`:

```toml
# Keep tokens private in ~/.config/herdr-telegram/env; reference env var names here.
[bots.ops]
token_env = "TELEGRAM_OPS_BOT_TOKEN"
title = "Herdr Ops Bridge"

# Fallback policy ("the rest") when no [[routes]] rule matches
[defaults]
bot = "default"
idle_delay_seconds = 90
blocked_delay_seconds = 30
fast_track_replies = true
silent_when_green = false

# First matching [[routes]] entry wins (workspace, agent, name, or title globs)
[[routes]]
name = "ops"
match_workspaces = ["prod-support", "on-duty", "ops-*", "incident-*"]
bot = "ops"
idle_delay_seconds = 0
silent_when_green = true
summary_style = "- Keep deploy and health-sweep bullets under 20 words."

# Add custom /spawn aliases or TUI status-bar adapters for any Herdr agent
[agents.opus]
kind = "claude"
spawn_args = ["--model", "opus"]
default_model = "opus"

[agents.codex-mini]
kind = "codex"
spawn_args = ["--no-alt-screen", "-m", "o4-mini"]
default_model = "o4-mini"
```

- **All 22 Herdr agent kinds supported:** `/spawn --agent <name>` works out of the box with `claude`, `codex`, `gemini`, `agy`, `opencode`, `cursor`, `pi`, `amp`, `droid`, `cline`, `copilot`, `devin`, `grok`, `hermes`, `kilo`, `kimi`, `kiro`, `maki`, `mastracode`, `omp`, `qodercli`, and `qwen`, plus any `[agents.<alias>]` profile you define (`spawn_args`, `model_patterns`, `prompt_prefixes`, `footer_patterns`, `default_model`).
- **Ordered `[[routes]]` + `[defaults]`:** Route panes across `1..N` Telegram bots by `match_workspaces`, `match_agents`, `match_names`, or `match_titles` globs, with per-route `idle_delay_seconds`, `blocked_delay_seconds`, `fast_track_replies`, `silent_when_green`, `hold_on_short_timers`, `suppress_support_done`, `include_output`, `summary_provider`, `summary_model`, and `summary_style`.
- **Per-channel model handles:** Model handles (`🤖 Opus`, `🤖 Sonnet`, `🤖 GPT5`) are numbered independently per routed bot channel (`Opus1`, `Opus2` only when multiple lead panes share a model on the same bot, ignoring background `/cowork` helper panes).

---

## Adaptable Summarizer Backends

For longer completed turns (`> 180` characters), `TurnSummarizer` extracts only the latest turn (including promptless cron/timer wakeups), redacts secrets first, and distills outcomes, passed checks (`🟢`), findings (`🔴 [P1]`, `🟡 [P2]`, `📝 [Note]`), and open questions.

By default (`HERDR_SUMMARY_PROVIDER=auto`), it uses whichever CLI or API key you already have configured, or falls back to clean HTML with zero setup:

| `HERDR_SUMMARY_PROVIDER` | How it runs | Default model | Auth / Config |
| :--- | :--- | :--- | :--- |
| `auto` *(default)* | Probes `claude`, `codex`, `gemini`, `agy`, then env API keys, else `off` | Provider default | Zero config |
| `claude` | `claude -p --setting-sources project --no-session-persistence --strict-mcp-config --tools ""` via stdin in empty temp dir | `haiku` | Existing Claude Code login or `ANTHROPIC_API_KEY` |
| `codex` | `codex exec --ephemeral --sandbox read-only` via stdin in empty temp dir | Codex config default | Existing Codex login or `OPENAI_API_KEY` |
| `gemini` | `gemini --approval-mode plan --admin-policy <deny-all.toml> -e none` via stdin in empty temp dir | `gemini-2.5-flash` | Existing Gemini CLI login or `GEMINI_API_KEY` |
| `openai` | `POST /chat/completions` (`HERDR_SUMMARY_API_URL` supports OpenRouter/Groq/vLLM) | `gpt-4.1-mini` | `HERDR_SUMMARY_API_KEY` or `OPENAI_API_KEY` |
| `ollama` | Local HTTP `http://127.0.0.1:11434/v1/chat/completions` | `llama3.2` | Local `ollama serve` |
| `anthropic` | `POST https://api.anthropic.com/v1/messages` | `claude-haiku-4-5` | `HERDR_SUMMARY_API_KEY` or `ANTHROPIC_API_KEY` |
| `gemini-api` | `POST .../v1beta/models/<model>:generateContent` | `gemini-2.5-flash` | `HERDR_SUMMARY_API_KEY` or `GEMINI_API_KEY` |
| `agy` | `agy --sandbox` with all tool permissions denied in empty temp dir | `gemini-2.5-flash` | Existing Antigravity CLI |
| `off` | Disables LLM calls; formats terminal output directly as clean HTML | None | None |

You can also use `provider:model` shorthand directly in `HERDR_SUMMARY_MODEL` (such as `HERDR_SUMMARY_MODEL=claude:haiku` or `HERDR_SUMMARY_MODEL=ollama:qwen2.5:7b`) or override `summary_provider` / `summary_model` per route in `config.toml`.

---

## Telegram Commands & Reply Routing

On startup, `herdr-telegram` automatically registers these commands in Telegram's native `/` menu picker across every configured bot:

| Action | Syntax | What it does |
| :--- | :--- | :--- |
| **Swipe-reply** | Swipe any alert or `telegram-send` card | Sends your reply (or photo/screenshot) straight to the bound pane (skips the idle completion wait) |
| **Model prefix** | `opus: run the auth tests` | Routes text or a photo caption to the `🤖 Opus` pane (case-insensitive; supports `opus2:`, `<workspace>:`, or `w12:p1:`) |
| **Send photo / screenshot** | Attach photo, album, or PNG/JPEG/WebP/GIF file | Saves images inside `<workspace>/.herdr-telegram/media` (`0600`, auto-excluded from `git status`), coalesces multi-photo albums into a single prompt, and prompts the target agent to inspect them |
| **List agents** | `/status` or `/status all` | Shows active panes on this bot channel (or all channels) with one-tap `📖` read buttons |
| **Read pane** | `/read [model]` | Reads the latest terminal scrollback from a pane |
| **Pin target** | `/target <model>` | Pins the default pane when multiple agents are open |
| **Send key** | `/key <model> <key>` | Sends allowed raw keys (`enter`, `esc`, `tab`, `space`, `1..9`, `y`, `n`, `a`, `p`, `ctrl+c`) |
| **Spawn repo** | `/spawn [-a <agent>] <repo> [prompt]` | Creates a Herdr workspace in `<repo>` under `HERDR_SPAWN_ROOTS` and starts any supported or custom agent (also works as a photo caption!) |

> **Tip for crisp terminal or log screenshots:** Send PNG or JPEG screenshots via Telegram's **Send as File** option to avoid photo compression. On iPhone, standard Telegram Photos auto-convert to JPEG; raw `.heic` file attachments are rejected with a reminder to resend as a Photo or PNG/JPEG.

---

## CLI Helpers

### `herdr-telegram`: Setup, Diagnostics, and Daemon

```bash
# Interactive 30-second pairing wizard (@BotFather token + /start auto-discovery + service start)
herdr-telegram setup

# Full diagnostic check (--json and --offline supported)
herdr-telegram doctor
```

### `telegram-send`: Proactive Agent Notifications & Photos

Agents (or CI/deploy scripts) can message you or send screenshots without ever reading your bot token:

```bash
# Uses $HERDR_PANE_ID automatically and routes via config.toml [[routes]]
telegram-send "Finished database migration; ready for smoke test?"

# Send a UI screenshot, chart, or diff image (swipe-replying to the photo routes back to the pane)
telegram-send --photo ./artifacts/login-mobile.png "Updated mobile layout; approve?"

# Force delivery through a specific named bot channel (such as --bot ops or --prod)
telegram-send --bot ops "🟢 HTTP health (12/12 endpoints healthy)"
```

If you installed with a custom `HERDR_TELEGRAM_CONFIG_DIR`, export `HERDR_TELEGRAM_CONFIG_DIR` in your shell profile as well so `telegram-send` reads the same directory as the background service.

### `ts-preview`: Mobile Dev Server Previews over Tailscale

Expose any `localhost` port to your phone over your tailnet and push a clickable link to Telegram:

```bash
# Expose localhost:5173 and text the tailnet URL to Telegram
ts-preview 5173 --telegram

# Expose a specific path with a custom label
ts-preview http://localhost:8080/docs/ --label "API Docs" --telegram

# Inspect active routes or check Tailscale Serve readiness
ts-preview status
ts-preview doctor

# Tear down one port or reset all previews
ts-preview off 5173
ts-preview reset
```

---

## Security & Safety Design

- **Private 1:1 authorization & trust boundary:** Rejects group/supergroup/channel updates and ignores any user whose numeric ID does not match `TELEGRAM_CHAT_ID`. Because your paired Telegram account can prompt and spawn local coding agents, treat your Telegram account (enable Two-Step Verification) and `~/.config/herdr-telegram/env` (`chmod 600`) as part of your workstation trust boundary.
- **Automatic secret redaction:** Strips private keys, OpenAI/Anthropic/GitHub/Slack/Telegram/AWS tokens, JWTs, Bearer headers, URL passwords, and `*_SECRET`/`*_API_KEY` assignments before text or captions reach either an LLM summarizer or Telegram. Note that pixels inside screenshots sent via `telegram-send --photo` cannot be text-redacted, so avoid screenshotting raw secret values.
- **Verified image handling & workspace isolation:** Inbound Telegram photos are downloaded only after chat authorization, freshness, and target-pane resolution succeed, verified by magic bytes, written atomically (`0600`) inside `<workspace>/.herdr-telegram/media` (refusing symlinks and creating `.herdr-telegram/.gitignore` plus `.git/info/exclude` so screenshots stay hidden across worktrees and subfolders), and pruned automatically (`7d` / `50` files / `200 MB`). Outbound `telegram-send --photo` opens files with `O_NOFOLLOW`, verifies regular-file `fstat` and image magic bytes (`JPEG`/`PNG`/`WebP`/`GIF`), and automatically switches between `sendPhoto` and `sendDocument` for tall full-page screenshots (`> 10000px`).
- **Isolated summarization & redirect-blocked HTTP:** CLI summarizers run inside a fresh empty temporary directory with tools/MCP disabled, prompts piped via `stdin`, and `TELEGRAM_BOT_TOKEN` scrubbed from child environments. Remote HTTP summarizers enforce `https://` (`http://` is permitted strictly on `127.0.0.1` / `localhost` / `::1`) and refuse all HTTP redirects so API headers are never forwarded across origins.
- **TOCTOU-guarded callbacks:** Inline approval buttons encode `state_change_seq`, a `terminal_id` tag, and a dialog-context digest, re-verifying live Herdr state before sending keys so a delayed tap can never approve a different command.
- **Loopback-only previews:** `ts-preview` refuses non-loopback targets and checks pre/post Tailscale Serve snapshots so stopping or adding a route never downgrades existing HTTPS listeners or wipes sibling paths.

---

## Development

Zero third-party runtime dependencies. Run formatting, lint, shellcheck, and the unit test suite locally:

```bash
uvx ruff format --check . herdr-telegram telegram-send ts-preview
uvx ruff check . herdr-telegram telegram-send ts-preview
shellcheck install.sh
python3 -m unittest discover -s . -v
```

## License

[MIT](LICENSE)
