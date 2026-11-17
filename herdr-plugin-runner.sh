#!/usr/bin/env bash
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_python311() {
  local cand resolved
  for cand in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      resolved="$(command -v "$cand")"
      if "$resolved" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        printf '%s\n' "$resolved"
        return 0
      fi
    fi
  done
  return 1
}

pause_if_tty() {
  if [ -t 0 ]; then
    printf '\nPress Enter to close...'
    read -r _ || true
  fi
}

service_unit_installed() {
  local os_name="${HERDR_TELEGRAM_OS_OVERRIDE:-$(uname -s)}"
  if [ "$os_name" = "Darwin" ]; then
    local launchd_dir="${HERDR_TELEGRAM_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
    [ -f "$launchd_dir/io.github.jzhao234.herdr-telegram.plist" ]
  else
    local systemd_dir="${HERDR_TELEGRAM_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
    [ -f "$systemd_dir/herdr-telegram.service" ]
  fi
}

ensure_standalone_install() {
  if service_unit_installed; then
    return 0
  fi
  echo "Installing persistent herdr-telegram background service and CLI shims..."
  # Pipe install.sh via stdin so BASH_SOURCE is empty: install.sh clones into
  # ~/.local/share/herdr-telegram instead of symlinking into Herdr's ephemeral plugin cache.
  bash -s <"$PLUGIN_DIR/install.sh"
}

main() {
  local subcmd="${1:-}"
  if [ -z "$subcmd" ]; then
    echo "Usage: herdr-plugin-runner.sh <pane-setup|pane-doctor|pane-setup-voice|doctor|restart-service|send-selection|send-pane-snapshot>" >&2
    exit 2
  fi
  shift

  local py_bin
  if ! py_bin="$(find_python311)"; then
    echo "Error: Python 3.11 or newer is required." >&2
    pause_if_tty
    exit 1
  fi

  case "$subcmd" in
    pane-setup)
      ensure_standalone_install || true
      "$py_bin" "$PLUGIN_DIR/herdr_telegram.py" setup "$@" || true
      pause_if_tty
      ;;
    pane-doctor)
      "$py_bin" "$PLUGIN_DIR/herdr_telegram.py" doctor "$@" || true
      pause_if_tty
      ;;
    pane-setup-voice)
      "$py_bin" "$PLUGIN_DIR/herdr_telegram.py" setup-voice "$@" || true
      pause_if_tty
      ;;
    doctor)
      exec "$py_bin" "$PLUGIN_DIR/herdr_telegram.py" doctor "$@"
      ;;
    restart-service)
      exec "$py_bin" - "$PLUGIN_DIR" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from bridge.cli import start_or_restart_background_service

ok, detail = start_or_restart_background_service()
stream = sys.stdout if ok else sys.stderr
print(detail, file=stream)
raise SystemExit(0 if ok else 1)
PY
      ;;
    send-selection)
      exec "$py_bin" - "$PLUGIN_DIR" <<'PY'
import json
import os
import sys

sys.path.insert(0, sys.argv[1])
import telegram_send

ctx_raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "").strip()
ctx = json.loads(ctx_raw) if ctx_raw else {}
text = str(ctx.get("selected_text") or "").strip()
if not text:
    print("Error: no terminal text selected.", file=sys.stderr)
    raise SystemExit(1)
if len(text) > 3500:
    text = text[:3500].rstrip() + "\n...[truncated]"

pane_id = str(ctx.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID") or "").strip()
argv = (["--pane", pane_id] if pane_id else []) + [f"Selected snippet:\n{text}"]
raise SystemExit(telegram_send.main(argv))
PY
      ;;
    send-pane-snapshot)
      exec "$py_bin" - "$PLUGIN_DIR" <<'PY'
import json
import os
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
import telegram_send

ctx_raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "").strip()
ctx = json.loads(ctx_raw) if ctx_raw else {}
pane_id = str(ctx.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID") or "").strip()
if not pane_id:
    print("Error: no focused Herdr pane ID available.", file=sys.stderr)
    raise SystemExit(1)

herdr_bin = os.environ.get("HERDR_BIN_PATH", "").strip() or "herdr"
proc = subprocess.run(
    [herdr_bin, "pane", "read", "--source", "recent-unwrapped", "--lines", "40", pane_id],
    capture_output=True,
    text=True,
    check=False,
    timeout=10,
)
if proc.returncode != 0:
    err = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
    print(f"Error reading pane {pane_id}: {err}", file=sys.stderr)
    raise SystemExit(1)

snippet = proc.stdout.strip()
if not snippet:
    snippet = "(pane buffer is currently empty)"
elif len(snippet) > 3500:
    snippet = "...[truncated]\n" + snippet[-3500:].lstrip()

raise SystemExit(telegram_send.main(["--pane", pane_id, snippet]))
PY
      ;;
    *)
      echo "Error: unknown plugin subcommand '$subcmd'." >&2
      exit 2
      ;;
  esac
}

main "$@"
