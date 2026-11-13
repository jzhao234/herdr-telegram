"""Interactive onboarding (`herdr-telegram setup`), diagnostics (`herdr-telegram doctor`), and service helpers."""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge.agents import HERDR_BUILTIN_AGENT_KINDS
from bridge.config import (
    default_config_toml_path,
    default_env_path,
    load_bridge_config,
    parse_env_file,
    seed_environ_from_env_file,
)

ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
LAUNCHD_LABEL = "io.github.jzhao234.herdr-telegram"
SYSTEMD_UNIT = "herdr-telegram.service"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str  # "ok", "warn", or "error"
    detail: str


def _format_env_value(value: str) -> str:
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError("Environment values must not contain newline or NUL characters.")
    if not value:
        return ""
    if re.search(r'[\s#"\'\\]', value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def upsert_env_values(
    env_path: Path,
    updates: Mapping[str, str],
    *,
    template_path: Path | None = None,
) -> None:
    """Atomically insert or update KEY=VALUE entries in env_path (0600) while preserving comments."""
    for key in updates:
        if not ENV_KEY_RE.match(key):
            raise ValueError(f"Invalid environment variable name: {key!r}")

    parent = env_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(parent, 0o700)

    if env_path.is_symlink():
        raise ValueError(f"Refusing to write through symlinked env file: {env_path}")

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif template_path is not None and template_path.is_file():
        lines = template_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = [
            "# Primary Telegram Bot token from @BotFather",
            "TELEGRAM_BOT_TOKEN=",
            "",
            "# Your numeric Telegram chat ID",
            "TELEGRAM_CHAT_ID=",
        ]

    remaining = dict(updates)
    seen_updated: set[str] = set()
    out_lines: list[str] = []
    key_pattern = re.compile(r"^(?P<lead>\s*(?:export\s+)?)(?P<key>[A-Z_][A-Z0-9_]*)\s*=(?P<rest>.*)$")

    for line in lines:
        m = key_pattern.match(line)
        if m:
            key = m.group("key")
            if key in remaining:
                formatted = _format_env_value(remaining.pop(key))
                seen_updated.add(key)
                out_lines.append(f"{m.group('lead')}{key}={formatted}")
                continue
            if key in seen_updated:
                # Drop duplicate later entries so last-wins parsers (systemd / parse_env_file) see the updated value
                continue
        out_lines.append(line)

    for key, val in remaining.items():
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append(f"{key}={_format_env_value(val)}")

    payload = ("\n".join(out_lines).rstrip("\n") + "\n").encode("utf-8")
    tmp_path = parent / f".{env_path.name}.tmp_{secrets.token_hex(6)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_path, flags, 0o600)
    closed = False
    try:
        mv = memoryview(payload)
        while mv:
            written = os.write(fd, mv)
            if written <= 0:
                raise OSError("Short write while saving env file")
            mv = mv[written:]
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.replace(tmp_path, env_path)
    except Exception:
        if not closed:
            with contextlib.suppress(OSError):
                os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def _current_os_name() -> str:
    override = os.environ.get("HERDR_TELEGRAM_OS_OVERRIDE", "").strip()
    return override or platform.system()


def default_launchd_plist_path() -> Path:
    raw = os.environ.get("HERDR_TELEGRAM_LAUNCHD_DIR", "").strip()
    base = Path(raw).expanduser() if raw else (Path.home() / "Library" / "LaunchAgents")
    return base / f"{LAUNCHD_LABEL}.plist"


def default_systemd_unit_path() -> Path:
    raw = os.environ.get("HERDR_TELEGRAM_SYSTEMD_DIR", "").strip()
    base = Path(raw).expanduser() if raw else (Path.home() / ".config" / "systemd" / "user")
    return base / SYSTEMD_UNIT


def get_service_status() -> tuple[str, str]:
    """Return ('ok'|'warn', detail) for the user's OS service manager."""
    os_name = _current_os_name()
    if os_name == "Darwin":
        plist_path = default_launchd_plist_path()
        if not plist_path.exists():
            return "warn", f"launchd plist not installed ({plist_path}); run ./install.sh"
        uid = os.getuid()
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0:
            return "ok", f"launchd {LAUNCHD_LABEL} loaded ({plist_path})"
        return "warn", f"launchd {LAUNCHD_LABEL} installed but not loaded ({plist_path})"

    unit_path = default_systemd_unit_path()
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return "warn", "systemctl not found on PATH"
    proc = subprocess.run(
        [systemctl, "--user", "is-active", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    state = (proc.stdout or proc.stderr).strip() or "inactive"
    if proc.returncode == 0 and state == "active":
        return "ok", f"systemd --user {SYSTEMD_UNIT} is active"
    if not unit_path.exists():
        return "warn", f"{SYSTEMD_UNIT} not installed ({unit_path}); run ./install.sh"
    return "warn", f"systemd --user {SYSTEMD_UNIT} is {state}"


def stop_background_service() -> None:
    """Best-effort stop of the background service to avoid Telegram getUpdates 409 conflicts during setup."""
    os_name = _current_os_name()
    if os_name == "Darwin":
        plist_path = default_launchd_plist_path()
        if plist_path.exists() and shutil.which("launchctl"):
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        return

    systemctl = shutil.which("systemctl")
    if systemctl:
        subprocess.run(
            [systemctl, "--user", "stop", SYSTEMD_UNIT],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )


def start_or_restart_background_service() -> tuple[bool, str]:
    """Enable and restart the user service on Linux (systemd) or macOS (launchd)."""
    os_name = _current_os_name()
    if os_name == "Darwin":
        plist_path = default_launchd_plist_path()
        if not plist_path.exists():
            return False, f"launchd plist not found at {plist_path} (run ./install.sh first)"
        if not shutil.which("launchctl"):
            return False, "launchctl not found"
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        boot = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if boot.returncode != 0:
            err = (boot.stderr or boot.stdout).strip()
            return False, f"launchctl bootstrap failed: {err}"
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return True, f"launchd {LAUNCHD_LABEL} started"

    systemctl = shutil.which("systemctl")
    unit_path = default_systemd_unit_path()
    if not systemctl:
        return False, "systemctl not found on PATH"
    if not unit_path.exists():
        return False, f"{unit_path} not found (run ./install.sh first)"
    subprocess.run([systemctl, "--user", "daemon-reload"], capture_output=True, text=True, check=False, timeout=10)
    en_proc = subprocess.run(
        [systemctl, "--user", "enable", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if en_proc.returncode != 0:
        err = (en_proc.stderr or en_proc.stdout).strip()
        return False, f"systemctl enable failed: {err}"
    re_proc = subprocess.run(
        [systemctl, "--user", "restart", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if re_proc.returncode != 0:
        err = (re_proc.stderr or re_proc.stdout).strip()
        return False, f"systemctl restart failed: {err}"
    return True, f"systemd --user {SYSTEMD_UNIT} enabled and active"


def run_doctor(
    *,
    as_json: bool = False,
    offline: bool = False,
    env_path: Path | None = None,
    config_path: Path | None = None,
    herdr_factory: Callable[[], Any] | None = None,
    telegram_factory: Callable[[str], Any] | None = None,
    service_status_fn: Callable[[], tuple[str, str]] = get_service_status,
) -> int:
    """Inspect local setup, permissions, Herdr connectivity, Telegram bots, and summarizer resolution."""
    import herdr_telegram as ht

    cfg_env = env_path if env_path is not None else default_env_path()
    cfg_toml = (
        config_path
        if config_path is not None
        else (cfg_env.parent / "config.toml" if env_path is not None else default_config_toml_path())
    )
    if env_path is None:
        seed_environ_from_env_file(cfg_env)

    checks: list[DoctorCheck] = []

    # 1. Python & OS
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(DoctorCheck("python", "ok", f"Python {py_ver} on {_current_os_name()}"))

    # 2. Env file & permissions
    if cfg_env.exists():
        try:
            mode = stat.S_IMODE(cfg_env.lstat().st_mode)
            if mode & 0o077:
                checks.append(
                    DoctorCheck(
                        "env_permissions",
                        "warn",
                        f"{cfg_env} has mode {oct(mode)} (run: chmod 600 {cfg_env})",
                    )
                )
            else:
                checks.append(DoctorCheck("env_permissions", "ok", f"{cfg_env} (mode {oct(mode)})"))
        except OSError as exc:
            checks.append(DoctorCheck("env_permissions", "error", f"Cannot stat {cfg_env}: {exc}"))
    else:
        checks.append(
            DoctorCheck(
                "env_permissions",
                "warn",
                f"{cfg_env} does not exist yet (run: herdr-telegram setup)",
            )
        )

    # 3. Configuration (env + config.toml)
    cfg = None
    effective_env: Mapping[str, str] = os.environ
    try:
        effective_env = os.environ if env_path is None else parse_env_file(cfg_env)
        cfg = load_bridge_config(config_path=cfg_toml, env_path=cfg_env, environ=effective_env)
        toml_note = f"loaded {cfg_toml}" if cfg_toml.exists() else "using defaults (no config.toml)"
        custom_agents = max(0, len(cfg.agent_registry.allowed_names) - len(HERDR_BUILTIN_AGENT_KINDS))
        checks.append(
            DoctorCheck(
                "config",
                "ok",
                f"{toml_note}; {len(cfg.bots)} bot(s), {len(cfg.rule_engine.routes)} route(s), {custom_agents} custom agent(s)",
            )
        )
    except Exception as exc:
        checks.append(DoctorCheck("config", "error", f"Invalid configuration: {ht.redact_secrets(str(exc))}"))

    # 4. Herdr CLI & daemon
    herdr_client = herdr_factory() if herdr_factory is not None else ht.HerdrClient()
    herdr_bin = getattr(herdr_client, "binary", "herdr")
    if herdr_factory is None and not (Path(herdr_bin).is_file() and os.access(herdr_bin, os.X_OK)):
        checks.append(
            DoctorCheck(
                "herdr",
                "error",
                f"Herdr binary not found at '{herdr_bin}' (install from https://github.com/herdrdev/herdr)",
            )
        )
    else:
        try:
            agents = herdr_client.list_agents()
            checks.append(DoctorCheck("herdr", "ok", f"{herdr_bin} reachable ({len(agents)} active agent pane(s))"))
        except Exception as exc:
            checks.append(
                DoctorCheck(
                    "herdr",
                    "warn",
                    f"{herdr_bin} found, but daemon not reachable yet ({ht.redact_secrets(str(exc))})",
                )
            )

    # 5. Telegram bot channels
    tg_cls = telegram_factory if telegram_factory is not None else ht.TelegramClient
    if cfg is not None:
        if "default" not in cfg.bots:
            checks.append(
                DoctorCheck(
                    "telegram",
                    "error",
                    f"TELEGRAM_BOT_TOKEN is not set in {cfg_env} (run: herdr-telegram setup)",
                )
            )
        else:
            for b_name, spec in cfg.bots.items():
                masked_cid = f"...{spec.chat_id[-4:]}" if len(spec.chat_id) > 4 else spec.chat_id
                pair_label = f"paired ({masked_cid})" if spec.chat_id else "unpaired (send /start or run setup)"
                pair_status = "ok" if spec.chat_id else "warn"
                if offline:
                    checks.append(DoctorCheck(f"bot:{b_name}", pair_status, f"token configured, {pair_label}"))
                    continue
                try:
                    me = tg_cls(spec.token).get_me()
                    uname = me.get("username")
                    handle = f"@{uname}" if uname else f"id={me.get('id', 'unknown')}"
                    checks.append(DoctorCheck(f"bot:{b_name}", pair_status, f"{handle} reachable ({pair_label})"))
                except Exception as exc:
                    checks.append(
                        DoctorCheck(
                            f"bot:{b_name}",
                            "error",
                            f"Telegram getMe failed: {ht.redact_secrets(str(exc))}",
                        )
                    )

        # 6. Summarizer backend
        defs = cfg.rule_engine.defaults
        if defs.summary_provider.lower() not in ht.SUPPORTED_SUMMARY_PROVIDERS:
            checks.append(DoctorCheck("summarizer", "error", f"Unsupported summary_provider={defs.summary_provider!r}"))
        else:
            prov, mod, bin_path, api_url, _ = ht.resolve_summary_backend(
                provider=defs.summary_provider,
                model=defs.summary_model,
                api_key=cfg.summary_api_key,
                api_url=cfg.summary_api_url,
                env=effective_env,
            )
            if prov == "off":
                checks.append(DoctorCheck("summarizer", "ok", "off (clean HTML terminal formatting)"))
            else:
                via = bin_path or ht._redact_url_for_log(api_url)
                label = f"{prov}:{mod}" if mod else prov
                checks.append(DoctorCheck("summarizer", "ok", f"{label} ({via})"))

        # 7. Spawn roots
        existing_roots = [str(p) for p in cfg.spawn_roots if p.expanduser().is_dir()]
        if existing_roots:
            checks.append(DoctorCheck("spawn_roots", "ok", ", ".join(existing_roots)))
        else:
            checks.append(
                DoctorCheck(
                    "spawn_roots",
                    "warn",
                    "No existing spawn_roots directories found (set HERDR_SPAWN_ROOTS or [bridge] spawn_roots for /spawn)",
                )
            )

    # 8. Background service
    svc_status, svc_detail = service_status_fn()
    checks.append(DoctorCheck("service", svc_status, svc_detail))

    has_error = any(c.status == "error" for c in checks)
    if as_json:
        print(
            json.dumps(
                {
                    "ok": not has_error,
                    "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
                },
                indent=2,
            )
        )
        return 1 if has_error else 0

    icons = {"ok": "[OK]  ", "warn": "[WARN]", "error": "[ERR] "}
    for c in checks:
        print(f"{icons.get(c.status, '[INFO]')} {c.name:16s} {c.detail}")
    return 1 if has_error else 0


def run_setup(
    *,
    env_path: Path | None = None,
    state_dir: Path | None = None,
    timeout_seconds: float = 120.0,
    input_fn: Callable[[str], str] = input,
    secret_input_fn: Callable[[str], str] = getpass.getpass,
    telegram_factory: Callable[[str], Any] | None = None,
    service_status_fn: Callable[[], tuple[str, str]] = get_service_status,
    stop_service_fn: Callable[[], None] = stop_background_service,
    start_service_fn: Callable[[], tuple[bool, str]] = start_or_restart_background_service,
    pair_code_factory: Callable[[], str] | None = None,
    require_tty: bool = True,
) -> int:
    """Interactive first-run wizard: verify @BotFather token, auto-pair via one-time /start token, save 0600 env, and start service."""
    import herdr_telegram as ht

    if require_tty and not sys.stdin.isatty():
        print(
            "Error: 'herdr-telegram setup' requires an interactive terminal. "
            f"For headless setup, write TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to {default_env_path()}.",
            file=sys.stderr,
        )
        return 2

    cfg_env = env_path if env_path is not None else default_env_path()
    if env_path is None:
        seed_environ_from_env_file(cfg_env)
    existing = parse_env_file(cfg_env)
    if state_dir is not None:
        target_state_dir = state_dir
    elif existing.get("HERDR_TELEGRAM_STATE_DIR", "").strip():
        target_state_dir = Path(existing["HERDR_TELEGRAM_STATE_DIR"].strip()).expanduser()
    else:
        target_state_dir = ht.default_state_dir()

    tg_cls = telegram_factory if telegram_factory is not None else ht.TelegramClient
    token = existing.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = existing.get("TELEGRAM_CHAT_ID", "").strip()
    bot_username = ""

    if token:
        try:
            me = tg_cls(token).get_me()
            bot_username = str(me.get("username") or "").strip()
            disp = f"@{bot_username}" if bot_username else "configured bot"
            ans = input_fn(f"Found existing Telegram bot {disp}. Keep this token? [Y/n]: ").strip().lower()
            if ans in ("n", "no"):
                token = ""
                chat_id = ""
        except urllib.error.HTTPError as exc:
            redacted = ht.redact_secrets(str(exc))
            if exc.code == 401:
                print(f"Existing TELEGRAM_BOT_TOKEN was rejected by Telegram ({redacted}); please enter a new one.")
                token = ""
                chat_id = ""
            else:
                print(f"Could not verify existing TELEGRAM_BOT_TOKEN ({redacted}).", file=sys.stderr)
                return 1
        except Exception as exc:
            redacted = ht.redact_secrets(str(exc))
            ans_replace = (
                input_fn(f"Could not verify existing token ({redacted}). Enter a different token instead? [y/N]: ")
                .strip()
                .lower()
            )
            if ans_replace in ("y", "yes"):
                token = ""
                chat_id = ""
            else:
                return 1

    if not token:
        print("1. Open Telegram, message @BotFather with /newbot, and copy your HTTP API token.")
        raw_tok = secret_input_fn("Paste TELEGRAM_BOT_TOKEN (input hidden): ").strip()
        if not raw_tok:
            print("Error: token cannot be empty.", file=sys.stderr)
            return 1
        try:
            me = tg_cls(raw_tok).get_me()
            bot_username = str(me.get("username") or "").strip()
        except Exception as exc:
            print(f"Error verifying token with Telegram: {ht.redact_secrets(str(exc))}", file=sys.stderr)
            return 1
        token = raw_tok

    client = tg_cls(token)
    with contextlib.suppress(Exception):
        set_cmds = getattr(client, "set_my_commands", None)
        if callable(set_cmds):
            set_cmds()

    if chat_id:
        ans_cid = input_fn(f"Found existing TELEGRAM_CHAT_ID={chat_id}. Keep it? [Y/n]: ").strip().lower()
        if ans_cid in ("n", "no"):
            chat_id = ""

    paired_update_id: int | None = None
    service_started_at_end = False
    was_service_active = False
    if not chat_id:
        pair_code = pair_code_factory() if pair_code_factory is not None else secrets.token_urlsafe(9)
        with contextlib.suppress(Exception):
            was_service_active = service_status_fn()[0] == "ok"
        stop_service_fn()
        try:
            offset = 0
            with contextlib.suppress(Exception):
                recent = client.get_updates(offset=-1, timeout=0)
                if recent:
                    offset = int(recent[-1]["update_id"]) + 1

            if bot_username:
                print(
                    f"2. Tap this one-time link on your phone (or send '/start {pair_code}' to @{bot_username}, "
                    f"waiting up to {int(timeout_seconds)}s):\n"
                    f"   https://t.me/{bot_username}?start={pair_code}"
                )
            else:
                print(
                    f"2. Open your bot in Telegram on your phone and send '/start {pair_code}' "
                    f"(waiting up to {int(timeout_seconds)}s)..."
                )

            deadline = time.monotonic() + max(2.0, timeout_seconds)
            while time.monotonic() < deadline:
                poll_wait = min(2, max(0, int(deadline - time.monotonic())))
                try:
                    updates = client.get_updates(offset=offset, timeout=poll_wait)
                except Exception as exc:
                    err_text = ht.redact_secrets(str(exc))
                    is_conflict = (isinstance(exc, urllib.error.HTTPError) and exc.code == 409) or "409" in err_text
                    hint = " (another instance is polling this bot or a webhook is configured)" if is_conflict else ""
                    print(f"Error polling Telegram updates: {err_text}{hint}", file=sys.stderr)
                    return 1
                for upd in updates:
                    upd_id = int(upd["update_id"])
                    offset = max(offset, upd_id + 1)
                    msg = upd.get("message") or {}
                    chat = msg.get("chat") or {}
                    text = str(msg.get("text") or "").strip()
                    cmd = ht.parse_slash_command(text) if text else None
                    if (
                        chat.get("type") == "private"
                        and cmd is not None
                        and cmd[0] == "start"
                        and cmd[1].strip() == pair_code
                        and chat.get("id") is not None
                    ):
                        chat_id = str(chat["id"])
                        paired_update_id = upd_id
                        break
                if chat_id:
                    break
                if poll_wait == 0:
                    break

            if not chat_id:
                upsert_env_values(cfg_env, {"TELEGRAM_BOT_TOKEN": token})
                print(
                    f"Timed out waiting for '/start {pair_code}'. Saved TELEGRAM_BOT_TOKEN to {cfg_env}; "
                    "re-run 'herdr-telegram setup' when ready to pair.",
                    file=sys.stderr,
                )
                return 1

            template_candidate = Path(__file__).resolve().parent.parent / "env.example"
            upsert_env_values(
                cfg_env,
                {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id},
                template_path=template_candidate if template_candidate.is_file() else None,
            )

            if paired_update_id is not None:
                state_path = target_state_dir / "state.json"
                st = ht.BridgeState.load(state_path)
                if paired_update_id > st.get_last_update_id("default"):
                    st.advance_update_id(state_path, paired_update_id, bot_role="default")
                with contextlib.suppress(Exception):
                    client.send_message(
                        chat_id,
                        "✅ <b>Paired with Herdr Telegram Bridge!</b>\n"
                        "Send <code>/status</code> anytime to inspect active agents, or swipe-reply to alerts.",
                    )

            started, svc_msg = start_service_fn()
            service_started_at_end = True
        finally:
            if not service_started_at_end and was_service_active:
                with contextlib.suppress(Exception):
                    start_service_fn()
    else:
        template_candidate = Path(__file__).resolve().parent.parent / "env.example"
        upsert_env_values(
            cfg_env,
            {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id},
            template_path=template_candidate if template_candidate.is_file() else None,
        )
        started, svc_msg = start_service_fn()

    print(f"Saved credentials to {cfg_env} (mode 0600, chat_id={chat_id}).")
    if started:
        print(f"Service ready: {svc_msg}")
    else:
        print(f"Note: {svc_msg}")
    return 0
