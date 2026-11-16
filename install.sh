#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [--install-skill] [--with-voice] [--uninstall [--purge]] [--help]

Options:
  --install-skill  Also symlink the bundled herdr-telegram SKILL.md into
                   ~/.agents/skills and ~/.claude/skills for coding agents.
  --with-voice     Optional: also install local offline faster-whisper voice transcription
                   (omitted by default so core install stays zero-dependency).
  --uninstall      Stop the background service and remove installed binaries/unit files
                   (preserves ~/.config/herdr-telegram/env and config.toml).
  --purge          With --uninstall, also remove ~/.config/herdr-telegram and state files.
  -h, --help       Show this help message.
EOF
}

validate_no_control_chars() {
  local name="$1"
  local val="$2"
  case "$val" in
    *$'\n'* | *$'\r'*)
      echo "Error: $name must not contain newline characters." >&2
      exit 1
      ;;
  esac
}

validate_safe_target_dir() {
  local label="$1"
  local dir="$2"
  local home_clean="${HOME%/}"
  local clean="${dir%/}"

  validate_no_control_chars "$label" "$dir"
  if [ -z "$clean" ] || [ "$clean" = "/" ] || [ "$clean" = "$home_clean" ]; then
    echo "Error: refusing unsafe $label='$dir'." >&2
    exit 1
  fi
  case "$clean" in
    /*/*) ;;
    *)
      echo "Error: $label must be an absolute path at least two levels deep (got '$dir')." >&2
      exit 1
      ;;
  esac
}

validate_install_dir() {
  local dir="$1"
  local cfg_clean="${CONFIG_DIR%/}"
  local bin_clean="${BIN_DIR%/}"
  local sysd_clean="${SYSTEMD_USER_DIR%/}"
  local launchd_clean="${LAUNCHD_USER_DIR%/}"
  local clean="${dir%/}"

  validate_safe_target_dir "HERDR_TELEGRAM_INSTALL_DIR" "$dir"
  validate_no_control_chars "HERDR_TELEGRAM_CONFIG_DIR" "$CONFIG_DIR"
  validate_no_control_chars "HERDR_TELEGRAM_BIN_DIR" "$BIN_DIR"
  validate_no_control_chars "HERDR_TELEGRAM_SYSTEMD_DIR" "$SYSTEMD_USER_DIR"
  validate_no_control_chars "HERDR_TELEGRAM_LAUNCHD_DIR" "$LAUNCHD_USER_DIR"
  validate_no_control_chars "HERDR_TELEGRAM_STATE_DIR" "$STATE_DIR"

  if [ "$clean" = "$cfg_clean" ] || [ "$clean" = "$bin_clean" ] || [ "$clean" = "$sysd_clean" ] || [ "$clean" = "$launchd_clean" ]; then
    echo "Error: refusing unsafe HERDR_TELEGRAM_INSTALL_DIR='$dir'." >&2
    exit 1
  fi
}

safe_remove_existing_install_dir() {
  local dir="$1"
  if [ -L "$dir" ]; then
    rm -f "$dir"
    return
  fi
  if [ ! -e "$dir" ]; then
    return
  fi
  if [ ! -d "$dir" ]; then
    echo "Error: '$dir' exists and is not a directory or symlink." >&2
    exit 1
  fi
  if [ -f "$dir/herdr_telegram.py" ] || [ -z "$(ls -A "$dir")" ]; then
    rm -rf "$dir"
  else
    echo "Error: refusing to overwrite non-empty directory '$dir' (does not look like a herdr-telegram install)." >&2
    exit 1
  fi
}

find_python311() {
  local cand
  for cand in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      local resolved
      resolved="$(command -v "$cand")"
      if "$resolved" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        printf '%s\n' "$resolved"
        return 0
      fi
    fi
  done
  return 1
}

run_uninstall() {
  local purge="$1"

  if [ "$OS_NAME" = "Darwin" ]; then
    local plist_path="$LAUNCHD_USER_DIR/io.github.jzhao234.herdr-telegram.plist"
    if [ "${HERDR_TELEGRAM_SKIP_LAUNCHCTL:-0}" != "1" ] && command -v launchctl >/dev/null 2>&1 && [ -f "$plist_path" ]; then
      launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
    fi
    rm -f "$plist_path"
  else
    if [ "${HERDR_TELEGRAM_SKIP_SYSTEMCTL:-0}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now herdr-telegram.service >/dev/null 2>&1 || true
    fi
    rm -f "$SYSTEMD_USER_DIR/herdr-telegram.service"
    if [ "${HERDR_TELEGRAM_SKIP_SYSTEMCTL:-0}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
      systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
  fi

  local cmd link_target install_clean="${INSTALL_DIR%/}"
  for cmd in herdr-telegram telegram-send ts-preview; do
    if [ -L "$BIN_DIR/$cmd" ]; then
      link_target="$(readlink "$BIN_DIR/$cmd" || true)"
      case "$link_target" in
        "$install_clean/"* | "$install_clean")
          rm -f "$BIN_DIR/$cmd"
          ;;
      esac
    fi
  done

  local skill_parent
  for skill_parent in "$AGENTS_SKILL_DIR" "$CLAUDE_SKILL_DIR"; do
    if [ -L "$skill_parent/herdr-telegram" ]; then
      link_target="$(readlink "$skill_parent/herdr-telegram" || true)"
      case "$link_target" in
        "$install_clean/"* | "$install_clean")
          rm -f "$skill_parent/herdr-telegram"
          ;;
      esac
    fi
  done

  safe_remove_existing_install_dir "$INSTALL_DIR"

  if [ "$purge" -eq 1 ]; then
    validate_safe_target_dir "HERDR_TELEGRAM_CONFIG_DIR" "$CONFIG_DIR"
    validate_safe_target_dir "HERDR_TELEGRAM_STATE_DIR" "$STATE_DIR"
    rm -f "$CONFIG_DIR/env" "$CONFIG_DIR/config.toml" "$CONFIG_DIR/config.example.toml"
    rmdir "$CONFIG_DIR" 2>/dev/null || true
    rm -f "$STATE_DIR/state.json" "$STATE_DIR/bridge.lock" "$STATE_DIR/bridge.log" "$STATE_DIR/bridge.log.1"
    rm -rf "${STATE_DIR:?}/media" "${STATE_DIR:?}/voice-venv"
    rmdir "$STATE_DIR" 2>/dev/null || true
    echo "Uninstalled herdr-telegram and purged bridge config/state files in $CONFIG_DIR and $STATE_DIR."
  else
    echo "Uninstalled herdr-telegram (preserved configuration in $CONFIG_DIR; pass --purge to remove it)."
  fi
}

main() {
  INSTALL_SKILL=0
  WITH_VOICE=0
  UNINSTALL=0
  PURGE=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --install-skill)
        INSTALL_SKILL=1
        ;;
      --with-voice)
        WITH_VOICE=1
        ;;
      --uninstall)
        UNINSTALL=1
        ;;
      --purge)
        PURGE=1
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        echo "Error: unknown option '$1'." >&2
        usage >&2
        exit 1
        ;;
    esac
    shift
  done

  if [ "$PURGE" -eq 1 ] && [ "$UNINSTALL" -ne 1 ]; then
    echo "Error: --purge can only be used together with --uninstall." >&2
    exit 1
  fi
  if { [ "$INSTALL_SKILL" -eq 1 ] || [ "$WITH_VOICE" -eq 1 ]; } && [ "$UNINSTALL" -eq 1 ]; then
    echo "Error: --install-skill and --with-voice cannot be combined with --uninstall." >&2
    exit 1
  fi

  OS_NAME="${HERDR_TELEGRAM_OS_OVERRIDE:-$(uname -s)}"
  case "$OS_NAME" in
    Linux | Darwin) ;;
    *)
      echo "Error: unsupported operating system '$OS_NAME' (expected Linux or Darwin/macOS)." >&2
      exit 1
      ;;
  esac

  REPO_URL="${HERDR_TELEGRAM_REPO_URL:-https://github.com/jzhao234/herdr-telegram.git}"
  DEFAULT_INSTALL_DIR="$HOME/.local/share/herdr-telegram"
  DEFAULT_CONFIG_DIR="$HOME/.config/herdr-telegram"
  DEFAULT_STATE_DIR="$HOME/.local/state/herdr-telegram"
  INSTALL_DIR="${HERDR_TELEGRAM_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
  BIN_DIR="${HERDR_TELEGRAM_BIN_DIR:-$HOME/.local/bin}"
  CONFIG_DIR="${HERDR_TELEGRAM_CONFIG_DIR:-$DEFAULT_CONFIG_DIR}"
  STATE_DIR="${HERDR_TELEGRAM_STATE_DIR:-$DEFAULT_STATE_DIR}"
  SYSTEMD_USER_DIR="${HERDR_TELEGRAM_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
  LAUNCHD_USER_DIR="${HERDR_TELEGRAM_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
  AGENTS_SKILL_DIR="${HERDR_TELEGRAM_AGENTS_SKILL_DIR:-$HOME/.agents/skills}"
  CLAUDE_SKILL_DIR="${HERDR_TELEGRAM_CLAUDE_SKILL_DIR:-$HOME/.claude/skills}"

  validate_install_dir "$INSTALL_DIR"
  validate_no_control_chars "HERDR_TELEGRAM_AGENTS_SKILL_DIR" "$AGENTS_SKILL_DIR"
  validate_no_control_chars "HERDR_TELEGRAM_CLAUDE_SKILL_DIR" "$CLAUDE_SKILL_DIR"

  if [ "$UNINSTALL" -eq 1 ]; then
    run_uninstall "$PURGE"
    return 0
  fi

  if ! PYTHON3_BIN="$(find_python311)"; then
    echo "Error: Python 3.11 or newer is required." >&2
    exit 1
  fi

  mkdir -p "$(dirname "$INSTALL_DIR")" "$BIN_DIR" "$CONFIG_DIR" "$STATE_DIR"
  chmod 700 "$CONFIG_DIR" "$STATE_DIR"

  SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
  LOCAL_REPO=""
  if [ -n "$SCRIPT_SOURCE" ] && [ -f "$SCRIPT_SOURCE" ]; then
    CANDIDATE_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
    if [ -f "$CANDIDATE_DIR/herdr_telegram.py" ] && [ -f "$CANDIDATE_DIR/herdr-telegram.service" ]; then
      LOCAL_REPO="$CANDIDATE_DIR"
    fi
  fi

  if [ -n "$LOCAL_REPO" ]; then
    if [ "$LOCAL_REPO" != "$INSTALL_DIR" ]; then
      if [ -e "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ]; then
        safe_remove_existing_install_dir "$INSTALL_DIR"
      fi
      ln -sfn "$LOCAL_REPO" "$INSTALL_DIR"
    fi
    SOURCE_DIR="$LOCAL_REPO"
  else
    if ! command -v git >/dev/null 2>&1; then
      echo "Error: git is required for one-line installation." >&2
      exit 1
    fi
    if [ -d "$INSTALL_DIR/.git" ] && [ ! -L "$INSTALL_DIR" ]; then
      echo "Updating existing installation at $INSTALL_DIR..."
      git -C "$INSTALL_DIR" pull --ff-only
    else
      safe_remove_existing_install_dir "$INSTALL_DIR"
      echo "Cloning herdr-telegram into $INSTALL_DIR..."
      git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    fi
    SOURCE_DIR="$INSTALL_DIR"
  fi

  chmod +x "$SOURCE_DIR/herdr_telegram.py" "$SOURCE_DIR/herdr-telegram" "$SOURCE_DIR/telegram-send" "$SOURCE_DIR/ts-preview"
  ln -sfn "$INSTALL_DIR/herdr-telegram" "$BIN_DIR/herdr-telegram"
  ln -sfn "$INSTALL_DIR/telegram-send" "$BIN_DIR/telegram-send"
  ln -sfn "$INSTALL_DIR/ts-preview" "$BIN_DIR/ts-preview"

  if [ "$OS_NAME" = "Darwin" ]; then
    mkdir -p "$LAUNCHD_USER_DIR"
    PLIST_PATH="$LAUNCHD_USER_DIR/io.github.jzhao234.herdr-telegram.plist"
    "$PYTHON3_BIN" - "$PLIST_PATH" "$PYTHON3_BIN" "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR" "$HOME" <<'PY'
import os
import plistlib
import sys

dst, py_bin, install_dir, config_dir, state_dir, home_dir = sys.argv[1:7]
payload = {
    "Label": "io.github.jzhao234.herdr-telegram",
    "ProgramArguments": [py_bin, f"{install_dir}/herdr_telegram.py"],
    "EnvironmentVariables": {
        "PYTHONUNBUFFERED": "1",
        "HERDR_TELEGRAM_CONFIG_DIR": config_dir,
        "HERDR_TELEGRAM_STATE_DIR": state_dir,
        "PATH": f"{home_dir}/.local/bin:{home_dir}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    },
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 15,
    "StandardOutPath": f"{state_dir}/bridge.log",
    "StandardErrorPath": f"{state_dir}/bridge.log",
}
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "wb") as fh:
    plistlib.dump(payload, fh, sort_keys=True)
PY
  else
    mkdir -p "$SYSTEMD_USER_DIR"
    if [ "$INSTALL_DIR" = "$DEFAULT_INSTALL_DIR" ] && [ "$CONFIG_DIR" = "$DEFAULT_CONFIG_DIR" ] && [ "$STATE_DIR" = "$DEFAULT_STATE_DIR" ]; then
      install -m 0644 "$SOURCE_DIR/herdr-telegram.service" "$SYSTEMD_USER_DIR/herdr-telegram.service"
    else
      "$PYTHON3_BIN" - "$SOURCE_DIR/herdr-telegram.service" "$SYSTEMD_USER_DIR/herdr-telegram.service" "$PYTHON3_BIN" "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR" <<'PY'
import os
import sys
from pathlib import Path


def escape_specifiers(val: str) -> str:
    return val.replace("%", "%%")


def systemd_quote(val: str) -> str:
    escaped = escape_specifiers(val).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


src, dst, py_bin, install_dir, config_dir, state_dir = sys.argv[1:7]
text = Path(src).read_text(encoding="utf-8")
# systemd EnvironmentFile= takes a raw unquoted absolute path after optional '-'
text = text.replace("-%h/.config/herdr-telegram/env", f"-{escape_specifiers(config_dir + '/env')}")
text = text.replace(
    "Environment=PYTHONUNBUFFERED=1",
    "Environment=PYTHONUNBUFFERED=1\n"
    f"Environment={systemd_quote('HERDR_TELEGRAM_CONFIG_DIR=' + config_dir)}\n"
    f"Environment={systemd_quote('HERDR_TELEGRAM_STATE_DIR=' + state_dir)}",
)
text = text.replace(
    "/usr/bin/env python3 %h/.local/share/herdr-telegram/herdr_telegram.py",
    f"{systemd_quote(py_bin)} {systemd_quote(install_dir + '/herdr_telegram.py')}",
)
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
    fi
    if [ "${HERDR_TELEGRAM_SKIP_SYSTEMCTL:-0}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
      systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
  fi

  if [ -f "$SOURCE_DIR/config.example.toml" ]; then
    install -m 0644 "$SOURCE_DIR/config.example.toml" "$CONFIG_DIR/config.example.toml"
  fi

  CREATED_ENV=0
  if [ ! -f "$CONFIG_DIR/env" ]; then
    install -m 0600 "$SOURCE_DIR/env.example" "$CONFIG_DIR/env"
    CREATED_ENV=1
  fi

  if [ "$INSTALL_SKILL" -eq 1 ] && [ -f "$SOURCE_DIR/skills/herdr-telegram/SKILL.md" ]; then
    local skill_dir
    for skill_dir in "$AGENTS_SKILL_DIR" "$CLAUDE_SKILL_DIR"; do
      mkdir -p "$skill_dir"
      if [ -e "$skill_dir/herdr-telegram" ] && [ ! -L "$skill_dir/herdr-telegram" ]; then
        echo "Warning: skipping $skill_dir/herdr-telegram (non-symlink directory already exists)." >&2
      else
        ln -sfn "$INSTALL_DIR/skills/herdr-telegram" "$skill_dir/herdr-telegram"
      fi
    done
    echo "Installed agent skill into $AGENTS_SKILL_DIR/herdr-telegram and $CLAUDE_SKILL_DIR/herdr-telegram."
  fi

  if [ "$WITH_VOICE" -eq 1 ]; then
    "$BIN_DIR/herdr-telegram" setup-voice
  fi

  echo "Installed herdr-telegram ($INSTALL_DIR) and CLI commands ($BIN_DIR/herdr-telegram, $BIN_DIR/telegram-send, $BIN_DIR/ts-preview)."

  if ! command -v herdr >/dev/null 2>&1 && [ ! -x "$HOME/.cargo/bin/herdr" ] && [ ! -x "$HOME/.local/bin/herdr" ] && [ ! -x "/opt/homebrew/bin/herdr" ]; then
    echo "Note: 'herdr' was not found on PATH yet. Install Herdr (https://github.com/herdrdev/herdr) before starting the service."
  fi

  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Note: add $BIN_DIR to your PATH to run 'herdr-telegram', 'telegram-send', and 'ts-preview' directly." ;;
  esac

  if [ "$CREATED_ENV" -eq 1 ]; then
    cat <<EOF

Next step (30-second interactive pairing):
  $BIN_DIR/herdr-telegram setup

Or configure $CONFIG_DIR/env manually and run '$BIN_DIR/herdr-telegram doctor' to verify.
EOF
  fi
}

main "$@"
