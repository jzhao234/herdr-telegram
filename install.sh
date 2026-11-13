#!/usr/bin/env bash
set -euo pipefail

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

validate_install_dir() {
  local dir="$1"
  local home_clean="${HOME%/}"
  local cfg_clean="${CONFIG_DIR%/}"
  local bin_clean="${BIN_DIR%/}"
  local sysd_clean="${SYSTEMD_USER_DIR%/}"
  local clean="${dir%/}"

  validate_no_control_chars "HERDR_TELEGRAM_INSTALL_DIR" "$dir"
  validate_no_control_chars "HERDR_TELEGRAM_CONFIG_DIR" "$CONFIG_DIR"
  validate_no_control_chars "HERDR_TELEGRAM_BIN_DIR" "$BIN_DIR"
  validate_no_control_chars "HERDR_TELEGRAM_SYSTEMD_DIR" "$SYSTEMD_USER_DIR"

  if [ -z "$clean" ] || [ "$clean" = "/" ] || [ "$clean" = "$home_clean" ] || [ "$clean" = "$cfg_clean" ] || [ "$clean" = "$bin_clean" ] || [ "$clean" = "$sysd_clean" ]; then
    echo "Error: refusing unsafe HERDR_TELEGRAM_INSTALL_DIR='$dir'." >&2
    exit 1
  fi
  case "$clean" in
    /*/*) ;;
    *)
      echo "Error: HERDR_TELEGRAM_INSTALL_DIR must be an absolute path at least two levels deep (got '$dir')." >&2
      exit 1
      ;;
  esac
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

main() {
  REPO_URL="${HERDR_TELEGRAM_REPO_URL:-https://github.com/jzhao234/herdr-telegram.git}"
  DEFAULT_INSTALL_DIR="$HOME/.local/share/herdr-telegram"
  DEFAULT_CONFIG_DIR="$HOME/.config/herdr-telegram"
  INSTALL_DIR="${HERDR_TELEGRAM_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
  BIN_DIR="${HERDR_TELEGRAM_BIN_DIR:-$HOME/.local/bin}"
  CONFIG_DIR="${HERDR_TELEGRAM_CONFIG_DIR:-$DEFAULT_CONFIG_DIR}"
  SYSTEMD_USER_DIR="${HERDR_TELEGRAM_SYSTEMD_DIR:-$HOME/.config/systemd/user}"

  validate_install_dir "$INSTALL_DIR"

  if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 (>= 3.11) is required." >&2
    exit 1
  fi
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Error: Python 3.11 or newer is required (found $(python3 --version 2>&1))." >&2
    exit 1
  fi

  mkdir -p "$(dirname "$INSTALL_DIR")" "$BIN_DIR" "$SYSTEMD_USER_DIR" "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"

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

  chmod +x "$SOURCE_DIR/herdr_telegram.py" "$SOURCE_DIR/telegram-send" "$SOURCE_DIR/ts-preview"
  ln -sfn "$INSTALL_DIR/telegram-send" "$BIN_DIR/telegram-send"
  ln -sfn "$INSTALL_DIR/ts-preview" "$BIN_DIR/ts-preview"

  if [ "$INSTALL_DIR" = "$DEFAULT_INSTALL_DIR" ] && [ "$CONFIG_DIR" = "$DEFAULT_CONFIG_DIR" ]; then
    install -m 0644 "$SOURCE_DIR/herdr-telegram.service" "$SYSTEMD_USER_DIR/herdr-telegram.service"
  else
    python3 - "$SOURCE_DIR/herdr-telegram.service" "$SYSTEMD_USER_DIR/herdr-telegram.service" "$INSTALL_DIR" "$CONFIG_DIR" <<'PY'
import os
import sys
from pathlib import Path


def escape_specifiers(val: str) -> str:
    return val.replace("%", "%%")


def systemd_quote(val: str) -> str:
    escaped = escape_specifiers(val).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


src, dst, install_dir, config_dir = sys.argv[1:5]
text = Path(src).read_text(encoding="utf-8")
# systemd EnvironmentFile= takes a raw unquoted absolute path after optional '-'
text = text.replace("-%h/.config/herdr-telegram/env", f"-{escape_specifiers(config_dir + '/env')}")
text = text.replace(
    "Environment=PYTHONUNBUFFERED=1",
    f"Environment=PYTHONUNBUFFERED=1\nEnvironment={systemd_quote('HERDR_TELEGRAM_CONFIG_DIR=' + config_dir)}",
)
text = text.replace(
    "%h/.local/share/herdr-telegram/herdr_telegram.py",
    systemd_quote(install_dir + "/herdr_telegram.py"),
)
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
  fi

  if [ -f "$SOURCE_DIR/config.example.toml" ]; then
    install -m 0644 "$SOURCE_DIR/config.example.toml" "$CONFIG_DIR/config.example.toml"
  fi

  CREATED_ENV=0
  if [ ! -f "$CONFIG_DIR/env" ]; then
    install -m 0600 "$SOURCE_DIR/env.example" "$CONFIG_DIR/env"
    CREATED_ENV=1
  fi

  if [ "${HERDR_TELEGRAM_SKIP_SYSTEMCTL:-0}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi

  echo "Installed herdr-telegram ($INSTALL_DIR) and CLI helpers ($BIN_DIR/telegram-send, $BIN_DIR/ts-preview)."

  if ! command -v herdr >/dev/null 2>&1 && [ ! -x "$HOME/.cargo/bin/herdr" ] && [ ! -x "$HOME/.local/bin/herdr" ]; then
    echo "Note: 'herdr' was not found on PATH yet. Install Herdr (https://github.com/herdrdev/herdr) before starting the service."
  fi

  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Note: add $BIN_DIR to your PATH to run 'telegram-send' and 'ts-preview' directly." ;;
  esac

  if [ "$CREATED_ENV" -eq 1 ]; then
    cat <<EOF

Next steps:
  1. Add your @BotFather token to $CONFIG_DIR/env (TELEGRAM_BOT_TOKEN=...)
  2. Start the bridge: systemctl --user enable --now herdr-telegram.service
  3. Send /start to your bot in Telegram to get your TELEGRAM_CHAT_ID, save it in $CONFIG_DIR/env, and run:
     systemctl --user restart herdr-telegram.service
  4. Optional: copy $CONFIG_DIR/config.example.toml to $CONFIG_DIR/config.toml to customize bots, rules, and agents.
EOF
  fi
}

main "$@"
