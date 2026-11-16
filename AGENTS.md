# AGENTS.md: Architecture, Setup, Configuration, and Extension Guide

`herdr-telegram` is a modular two-way Telegram bridge and Tailscale mobile preview toolkit for [Herdr](https://github.com/herdrdev/herdr) terminal coding agents on Linux (`systemd --user`) and macOS (`launchd`).

This document is written so a human contributor or a coding agent (`claude`, `codex`, `gemini`, `agy`, `opencode`, `cursor`) pointed at this repository can set it up, configure optional modules, or modify the codebase without guesswork.

---

## 1. Core Design Philosophy & Modularity

The core bridge runs on **pure Python 3.11+ standard library (`dependencies = []`)**. Every subsystem beyond basic two-way Telegram text and approval buttons is **modular and optional**:

| Subsystem | Module(s) | Default State | How to Enable / Customize | How to Disable Completely |
| :--- | :--- | :--- | :--- | :--- |
| **Core 2-way text & blocked buttons** | `herdr_telegram.py`, `bridge/rules.py`, `bridge/agents.py` | Always enabled | Works out of the box with `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Stop service (`install.sh --uninstall`) |
| **LLM completion digests** | `TurnSummarizer` in `herdr_telegram.py` | `summary_provider = "auto"` | Auto-detects installed CLI (`claude`, `codex`, `gemini`, `agy`) or API key; falls back to clean HTML if none exist | Set `summary_provider = "off"` in `config.toml` (or `HERDR_SUMMARY_PROVIDER=off`) |
| **Voice note transcription** | `bridge/voice.py` | `voice_provider = "auto"` (dormant until configured; 0 MB RAM/disk) | Run `herdr-telegram setup-voice` for local offline `faster-whisper`, or set `HERDR_VOICE_API_KEY` (`openai`, `groq`, `gemini-api`) | Leave uninstalled, or set `voice_provider = "off"` (`HERDR_VOICE_PROVIDER=off`) |
| **Inbound & outbound photos** | `bridge/media.py`, `telegram_send.py` | Stdlib-only (runs only when photos are sent) | Attach photos in Telegram or run `telegram-send --photo <path>` | Simply do not send photos; zero background overhead |
| **Multi-bot & custom routing** | `bridge/config.py`, `bridge/rules.py` | Single `default` bot | Define `[bots.<name>]` and `[[routes]]` in `~/.config/herdr-telegram/config.toml` | Omit `config.toml` |
| **Custom `/spawn` agent profiles** | `bridge/agents.py`, `bridge/config.py` | 22 built-in Herdr agent kinds | Add `[agents.<alias>]` tables in `~/.config/herdr-telegram/config.toml` | Omit `[agents.*]` |
| **Tailscale mobile dev previews** | `ts_preview.py`, `ts-preview` | Standalone on-demand CLI | Run `ts-preview <port> --telegram` | Unused unless invoked |

---

## 2. Repository & Module Map

```text
herdr-telegram/
├── herdr_telegram.py        # Main daemon loop (BridgeDaemon), HerdrClient, TelegramClient,
│                            # TurnSummarizer, blocked-dialog parser, '/' commands & callbacks
├── telegram_send.py         # Standalone proactive sender CLI ('telegram-send' & '--photo')
├── ts_preview.py            # Standalone Tailscale Serve mobile preview CLI ('ts-preview')
├── herdr-telegram           # Thin Python >=3.11 wrapper entrypoint for daemon/setup/doctor
├── telegram-send            # Thin wrapper entrypoint for telegram_send.py
├── ts-preview               # Thin wrapper entrypoint for ts_preview.py
├── bridge/
│   ├── __init__.py          # Public package exports and __version__
│   ├── agents.py            # 22 built-in Herdr agent specs + custom [agents.<name>] regex/flag adapter
│   ├── rules.py             # BotChannelSpec, RouteDefaults, RouteRule, and ordered glob RuleEngine
│   ├── config.py            # ~/.config/herdr-telegram/env + config.toml loader & validator
│   ├── media.py             # Image magic-byte sniffing, 0600 .herdr-telegram/media storage, multipart
│   ├── voice.py             # Optional voice/audio sniffing, local faster-whisper + cloud STT, spoken prefixes
│   └── cli.py               # 'setup', 'setup-voice', 'doctor', env atomic writer, systemd/launchd helpers
├── install.sh               # Idempotent Linux/macOS installer, skill installer, and uninstaller
├── herdr-telegram.service   # Linux systemd --user unit template
├── config.example.toml      # Annotated modular TOML configuration template
├── env.example              # Annotated ~/.config/herdr-telegram/env template
├── skills/herdr-telegram/   # Bundled SKILL.md for Claude Code, Codex, Gemini, and Antigravity
├── test_herdr_telegram.py   # End-to-end & unit tests for bridge, config, CLI, media, voice, installer
└── test_ts_preview.py       # Unit tests for Tailscale Serve URL/port/state safety
```

### Runtime Data Flow

1. **Outbound (Herdr -> Phone):** `BridgeDaemon.poll_herdr_once()` polls `HerdrClient.list_agents()`. Each `(pane_id, terminal_id)` state transition is evaluated against `RuleEngine.resolve(snap)` (`bridge/rules.py`). After `idle_delay_seconds` (skipped when `fast_track_replies` is armed by a user reply) or `blocked_delay_seconds`, the daemon formats either a completion digest (via `TurnSummarizer` or deterministic HTML) or an interactive `Blocked` card (`extract_blocked_menu` -> inline buttons) and sends it via the routed `TelegramClient`.
2. **Inbound (Phone -> Herdr):** `BridgeDaemon.handle_update()` verifies private-chat authorization (`TELEGRAM_CHAT_ID`) and message freshness (`<= 300s`), dispatches `/` commands, inline callbacks (`pin:`, `read:`, `kpick:`, `kraw:`, `spag:`, `sprp:`, `vsend:`, `key:`, `opt:`), photos (`_deliver_image_batch`), or voice notes (`_deliver_voice_message`), resolves the target pane (`resolve_reply_target` / `resolve_canonical_pane_id`), attaches `[Replying to ...]` context via `attach_reply_context()` whenever the user swipe-replies to a non-latest message or highlights a Telegram quote, and calls `HerdrClient.prompt_agent_detailed()` or `send_keys_detailed()`.
3. **Space Naming Convention:** Each workspace's lead pane is addressed as `<space>` (`/target_upgrade`, `upgrade: <prompt>`), while review/secondary panes in that workspace are addressed as `<space>_<model>` (`upgrade_opus`) or `<space>_<model>_#` (`migration_gpt5_1`, `migration_gpt5_2`) when two or more review panes share a model in the same workspace.

---

## 3. Agent-Assisted Setup & Diagnostics Playbook

When a user asks an agent to install, configure, or troubleshoot `herdr-telegram`:

1. **Never read or print raw secret values** (`TELEGRAM_BOT_TOKEN`, `*_API_KEY`) from `~/.config/herdr-telegram/env`. Use `herdr-telegram doctor --json --offline` (or `herdr-telegram doctor`) to inspect health safely with masked IDs and redacted URLs.
2. **Install or update from a local checkout:**
   ```bash
   ./install.sh --install-skill
   ```
   When run inside a git clone, `install.sh` symlinks `~/.local/share/herdr-telegram` straight to the checkout so code edits take effect immediately upon restarting the service.
3. **First-time pairing:** Have the user run `herdr-telegram setup` in an interactive terminal (hidden token prompt + one-click `/start` phone pairing), or use `bridge.cli.upsert_env_values` if writing non-secret settings programmatically (`0600` atomic write).
4. **Optional local voice setup:** Run `herdr-telegram setup-voice` (or `herdr-telegram setup-voice --model small.en` on low-RAM machines) only if the user wants voice note transcription.
5. **Apply config or code changes:**
   - Linux: `systemctl --user restart herdr-telegram.service && systemctl --user is-active herdr-telegram.service`
   - macOS: `launchctl kickstart -k "gui/$(id -u)/io.github.jzhao234.herdr-telegram"`
   - Verify: `herdr-telegram doctor --offline`

---

## 4. Complete Configuration Surface

Configuration merges two files in `~/.config/herdr-telegram/` (override directory with `HERDR_TELEGRAM_CONFIG_DIR`):
- **`env` (`chmod 600`):** Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, named bot tokens, optional API keys) and optional `HERDR_*` overrides.
- **`config.toml` (`0644`, optional):** Declarative tables for `[bridge]`, `[bots.<name>]`, `[defaults]`, `[[routes]]`, and `[agents.<alias>]`. Explicit non-empty environment variables override matching scalar keys.

### `[bridge]` Table & Global Env Vars

| `config.toml` (`[bridge]`) | `env` Variable | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `poll_interval` | `HERDR_POLL_INTERVAL` | `2.0` | Seconds between Herdr state polls (`> 0`) |
| `read_lines` | `HERDR_READ_LINES` | `120` | Terminal scrollback lines returned by `/read` |
| `summary_read_lines` | `HERDR_SUMMARY_READ_LINES` | `180` | Terminal lines inspected when extracting the last turn |
| `summary_min_chars` | `HERDR_SUMMARY_MIN_CHARS` | `180` | Minimum turn length before calling `TurnSummarizer` |
| `default_agent` | `HERDR_DEFAULT_AGENT` | `"claude"` | Default agent kind or custom `[agents.<alias>]` for `/spawn` |
| `spawn_roots` | `HERDR_SPAWN_ROOTS` | Auto-discovered | Directories searched by `/spawn` (`:`-separated in env, list in TOML) |
| `spawn_timeout_ms` | `HERDR_SPAWN_TIMEOUT_MS` | `30000` | Readiness timeout for `/spawn` (`3001..60000` ms) |

### `[defaults]` & Per-Rule `[[routes]]` Keys

First matching `[[routes]]` rule wins (`match_workspaces`, `match_agents`, `match_names`, `match_titles` support case-insensitive globs; non-empty `match_*` fields are ANDed together, items inside a list are ORed). Unmatched panes use `[defaults]`:

- `bot`: Target bot channel name (`"default"` or any `[bots.<name>]` key).
- `idle_delay_seconds` (`HERDR_IDLE_DELAY_SECONDS`, default `90`): Quiet window before sending a `Done` digest (`0` for instant).
- `blocked_delay_seconds` (`HERDR_BLOCKED_DELAY_SECONDS`, default `30`): Debounce before sending a `Blocked` alert (`0` for instant).
- `fast_track_replies` (default `true`): Skip `idle_delay_seconds` on the turn immediately following a Telegram prompt or button press.
- `silent_when_green` (default `false`): Send `disable_notification=true` when a digest has no `P1`/`P2` findings, no PR changes, and no `attention` questions.
- `hold_on_short_timers` (default `true`): Hold mid-workflow `Done` digests when the agent explicitly scheduled a short `<5m` wake-up timer.
- `suppress_support_done` (default `true`): Suppress routine `Done` digests from `/cowork` helper panes (`*-support-*`, `cx-w*-p*`) while still alerting if they become `blocked`.
- `include_output` (`HERDR_INCLUDE_OUTPUT`, default `true`): Include terminal output/summaries and TUI model detection (`false` sends status-only headers without reading terminal text).
- `summary_provider` (`HERDR_SUMMARY_PROVIDER`, default `"auto"`): `auto`, `off`, `claude`, `codex`, `gemini`, `agy`, `openai`, `ollama`, `anthropic`, or `gemini-api`.
- `summary_model` (`HERDR_SUMMARY_MODEL`, default `""`): Model name or `provider:model` shorthand (e.g. `"claude:haiku"`).
- `summary_style` (default `""`): Extra bullet formatting instructions appended to `TurnSummarizer` for that route.
- `voice_provider` (`HERDR_VOICE_PROVIDER`, default `"auto"`, top-level `[defaults]`/`[bridge]`): `auto`, `off`, `local` (`faster-whisper`), `openai`, `groq`, or `gemini-api`.
- `voice_model` (`HERDR_VOICE_MODEL`, default `""`): Whisper/STT model override (defaults to `large-v3-turbo` locally, `gpt-4o-mini-transcribe` on OpenAI, `whisper-large-v3-turbo` on Groq, `gemini-2.5-flash` on Gemini).

### `[bots.<name>]` & `[agents.<alias>]`

- **`[bots.<name>]`:** Name channels generically or to match the user's workflow (e.g. `[bots.alerts]`, `[bots.ci]`). Set `token_env = "TELEGRAM_ALERTS_BOT_TOKEN"` (and optional `chat_id_env`, `title`) so tokens stay in `~/.config/herdr-telegram/env` while `config.toml` stays safe to commit to dotfiles. When `title` is omitted, `"default"` uses `"Herdr Telegram Bridge"` and any named channel `<name>` uses `"Herdr Bridge (<name>)"`.
- **`[agents.<alias>]`:** Define `kind` (one of the 22 built-in Herdr agent kinds in `bridge/agents.py`), `spawn_args = [...]`, `default_model = "..."`, and optional TUI regex overrides (`model_patterns`, `prompt_prefixes`, `footer_patterns`).

---

## 5. Extension Playbooks (How to Modify the Code)

### Adding a New `/slash` Command or Inline Picker
1. Add the static command tuple `(name, description)` to `BOT_MENU_COMMANDS` in `herdr_telegram.py` (Telegram `BotCommand` names must match `^[a-z0-9_]{1,32}$` and descriptions must be `1..256` chars).
2. Handle `if cmd == "<name>":` inside `BridgeDaemon.handle_update()`.
3. If adding inline keyboard buttons, keep `callback_data` **<= 64 UTF-8 bytes** (Telegram's hard limit; use short prefixes or 8-hex tokens like `sprp:` / `vsend:`) and handle the prefix in `BridgeDaemon._handle_callback()` (which already enforces `_is_authorized_callback`). Use `self._edit_or_send(...)` to update picker cards in place.

### Adding a New TUI Agent Adapter
- If the agent is already supported by `herdr agent start <kind>`, check `HERDR_BUILTIN_AGENT_KINDS` in `bridge/agents.py`. Add default status-bar regexes (`model_patterns`), user prompt markers (`prompt_prefixes`), or TUI chrome filters (`footer_patterns`) to `DEFAULT_AGENT_SPECS` in `bridge/agents.py` (users can also add `[agents.<name>]` in `config.toml` with zero code changes).

### Adding a Summarizer or Voice Provider
- **Summarizer:** Update `SUPPORTED_SUMMARY_PROVIDERS` and `resolve_summary_backend()` in `herdr_telegram.py`, then implement execution in `TurnSummarizer._summarize_via_cli()` or `TurnSummarizer._summarize_via_http()`. Always pass `TurnSummarizer._build_scrubbed_env()` to subprocesses and use `_open_summary_http` (no HTTP redirects, HTTPS-only except loopback).
- **Voice STT:** Update `SUPPORTED_VOICE_PROVIDERS`, `resolve_voice_backend()`, and `VoiceTranscriber.transcribe_bytes()` in `bridge/voice.py`. Keep third-party libraries strictly out of top-level imports so `dependencies = []` stays true.

### Customizing Notification Formatting
- Completion cards: `TurnSummarizer._build_prompt()`, `parse_digest_json()`, and `format_done_message()` in `herdr_telegram.py`.
- Blocked prompt cards & option buttons: `extract_blocked_menu()`, `format_blocked_dialog_html()`, and `build_transition_keyboard()` in `herdr_telegram.py`.

---

## 6. Security Invariants & Required Checks

Preserve these invariants whenever modifying the codebase:
- **Zero mandatory third-party imports** in `herdr_telegram.py`, `telegram_send.py`, `ts_preview.py`, and `bridge/*.py` (stdlib Python 3.11+ only).
- **Auth & freshness first:** Never download Telegram files, run subprocesses, or mutate state before `_is_authorized_message` / `_is_authorized_callback` and `MAX_MESSAGE_AGE_SECONDS` pass.
- **Secret hygiene:** Run `redact_secrets()` before sending terminal text to any external LLM or Telegram API, scrub `TELEGRAM_BOT_TOKEN` from child subprocess environments, write secret/media files with `0600` + `O_NOFOLLOW`, and block HTTP redirects on authenticated requests.
- **TOCTOU safety on blocked approvals:** Approval callbacks (`key:` and `opt:`) must continue verifying live `state_change_seq`, `terminal_id` tag, and dialog digest via `_verify_live_blocked_callback` before sending keystrokes.
- **No em dashes** in user-facing strings, templates, or documentation (`strip_em_dashes()` enforces this at runtime).

Run all four checks from the repository root before considering any change complete:

```bash
uvx ruff format --check . herdr-telegram telegram-send ts-preview
uvx ruff check . herdr-telegram telegram-send ts-preview
shellcheck install.sh
python3 -m unittest discover -s . -v
```
