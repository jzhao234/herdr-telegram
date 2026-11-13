"""TOML + env configuration loader for bots, routes, and custom agent adapters."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge.agents import AgentRegistry, AgentSpec
from bridge.rules import BotChannelSpec, RouteDefaults, RouteRule, RuleEngine

LEGACY_PROD_WORKSPACES: tuple[str, ...] = (
    "prod-support",
    "prodsupport",
    "production-support",
    "on-duty",
    "onduty",
    "oncall",
    "incident-response",
)

BOT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def default_config_dir() -> Path:
    raw = os.environ.get("HERDR_TELEGRAM_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "herdr-telegram"


def default_env_path() -> Path:
    return default_config_dir() / "env"


def default_config_toml_path() -> Path:
    raw = os.environ.get("HERDR_TELEGRAM_CONFIG", "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_config_dir() / "config.toml"


def _strip_unquoted_comment(raw_val: str) -> str:
    val = raw_val.strip()
    if not val:
        return ""
    if val[0] in ('"', "'"):
        quote = val[0]
        escaped = False
        for idx in range(1, len(val)):
            ch = val[idx]
            if escaped:
                escaped = False
                continue
            if ch == "\\" and quote == '"':
                escaped = True
                continue
            if ch == quote:
                rest = val[idx + 1 :].strip()
                if not rest or rest.startswith("#"):
                    inner = val[1:idx]
                    return inner.replace('\\"', '"').replace("\\\\", "\\") if quote == '"' else inner
                break
    match = re.search(r"\s+#", val)
    if match:
        val = val[: match.start()].rstrip()
    return val


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines, supporting optional 'export ' prefixes, quotes, and inline # comments."""
    env_vars: dict[str, str] = {}
    if not path.exists():
        return env_vars
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export ") or stripped.startswith("export\t"):
            stripped = stripped[6:].lstrip()
        if "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        clean_key = key.strip()
        if clean_key:
            env_vars[clean_key] = _strip_unquoted_comment(val)
    return env_vars


def seed_environ_from_env_file(
    env_path: Path | None = None,
    *,
    target_environ: MutableMapping[str, str] | None = None,
) -> None:
    """Populate missing environment variables from env file (parity across systemd, launchd, and CLI runs)."""
    path = env_path if env_path is not None else default_env_path()
    dest = os.environ if target_environ is None else target_environ
    for key, val in parse_env_file(path).items():
        if val and key not in dest:
            dest[key] = val


def _as_str_tuple(val: Any, field_name: str) -> tuple[str, ...]:
    if val is None:
        return ()
    if isinstance(val, str):
        items = [part.strip() for part in re.split(r"[,:]", val) if part.strip()]
        return tuple(items)
    if isinstance(val, list):
        out: list[str] = []
        for item in val:
            if not isinstance(item, str):
                raise ValueError(f"Expected string items in {field_name}, got {type(item).__name__}")
            if item.strip():
                out.append(item.strip())
        return tuple(out)
    raise ValueError(f"Expected list of strings for {field_name}, got {type(val).__name__}")


def _as_strict_list_tuple(val: Any, field_name: str) -> tuple[str, ...]:
    """Parse a TOML array of strings without splitting on commas or colons (safe for regexes and CLI flags)."""
    if val is None:
        return ()
    if not isinstance(val, list):
        raise ValueError(f"Expected TOML array of strings for {field_name}, got {type(val).__name__}")
    out: list[str] = []
    for item in val:
        if not isinstance(item, str):
            raise ValueError(f"Expected string items in {field_name}, got {type(item).__name__}")
        out.append(item)
    return tuple(out)


def _pick_raw(env: Mapping[str, str], env_key: str, tbl: Mapping[str, Any], toml_key: str, default: Any) -> Any:
    env_val = env.get(env_key, "").strip()
    if env_val != "":
        return env_val
    toml_val = tbl.get(toml_key)
    if toml_val is not None:
        return toml_val
    return default


def _as_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"Invalid boolean value {val!r}")


def _as_opt_bool(val: Any) -> bool | None:
    if val is None:
        return None
    return _as_bool(val, False)


def _as_opt_nonneg_float(val: Any, field_name: str) -> float | None:
    if val is None:
        return None
    f = float(val)
    if f < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return f


@dataclass(frozen=True)
class BridgeConfig:
    bots: dict[str, BotChannelSpec]
    rule_engine: RuleEngine
    agent_registry: AgentRegistry
    poll_interval: float
    read_lines: int
    summary_read_lines: int
    summary_min_chars: int
    default_agent: str
    spawn_roots: tuple[Path, ...]
    spawn_timeout_ms: int
    summary_api_key: str = ""
    summary_api_url: str = ""


def _default_spawn_roots_from_env(raw: str) -> tuple[Path, ...]:
    if raw.strip():
        roots = tuple(Path(p.strip()).expanduser() for p in raw.split(":") if p.strip())
        if roots:
            return roots
    home = Path.home()
    found = tuple(
        home / name
        for name in ("projects", "Projects", "code", "Code", "src", "repos", "dev")
        if (home / name).is_dir()
    )
    return found or (home / "projects", home / "code")


def load_bridge_config(
    config_path: Path | None = None,
    *,
    env_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> BridgeConfig:
    """Load unified BridgeConfig from optional config.toml + env file + process environment."""
    resolved_env_path = env_path if env_path is not None else default_env_path()
    file_env = parse_env_file(resolved_env_path)
    proc_env = os.environ if environ is None else environ
    merged_env: dict[str, str] = {**file_env, **{k: v for k, v in proc_env.items() if v is not None}}

    resolved_toml_path = config_path if config_path is not None else default_config_toml_path()
    toml_data: dict[str, Any] = {}
    if resolved_toml_path.exists():
        toml_data = tomllib.loads(resolved_toml_path.read_text(encoding="utf-8"))

    # 1. Numeric & global settings (env overrides config.toml [bridge] table when explicitly set)
    bridge_tbl = toml_data.get("bridge") or {}
    if not isinstance(bridge_tbl, dict):
        raise ValueError("[bridge] in config.toml must be a table")

    poll_interval = float(_pick_raw(merged_env, "HERDR_POLL_INTERVAL", bridge_tbl, "poll_interval", 2.0))
    read_lines = int(_pick_raw(merged_env, "HERDR_READ_LINES", bridge_tbl, "read_lines", 120))
    summary_read_lines = int(_pick_raw(merged_env, "HERDR_SUMMARY_READ_LINES", bridge_tbl, "summary_read_lines", 180))
    summary_min_chars = int(_pick_raw(merged_env, "HERDR_SUMMARY_MIN_CHARS", bridge_tbl, "summary_min_chars", 180))
    spawn_timeout_ms = int(_pick_raw(merged_env, "HERDR_SPAWN_TIMEOUT_MS", bridge_tbl, "spawn_timeout_ms", 30000))

    if poll_interval <= 0 or read_lines <= 0 or summary_read_lines <= 0:
        raise ValueError("poll_interval, read_lines, and summary_read_lines must be positive")
    if summary_min_chars < 0:
        raise ValueError("summary_min_chars must be non-negative")
    if not (3001 <= spawn_timeout_ms <= 60000):
        raise ValueError("spawn_timeout_ms must be between 3001 and 60000")

    # 2. Custom [agents.<name>] registry
    agents_tbl = toml_data.get("agents") or {}
    if not isinstance(agents_tbl, dict):
        raise ValueError("[agents] in config.toml must be a table")
    custom_specs: list[AgentSpec] = []
    for alias, raw_spec in agents_tbl.items():
        if not isinstance(raw_spec, dict):
            raise ValueError(f"[agents.{alias}] must be a table")
        custom_specs.append(
            AgentSpec(
                name=str(alias),
                kind=str(raw_spec.get("kind") or alias),
                spawn_args=_as_strict_list_tuple(raw_spec.get("spawn_args"), f"agents.{alias}.spawn_args"),
                model_patterns=_as_strict_list_tuple(raw_spec.get("model_patterns"), f"agents.{alias}.model_patterns"),
                prompt_prefixes=tuple(
                    p.strip()
                    for p in _as_strict_list_tuple(raw_spec.get("prompt_prefixes"), f"agents.{alias}.prompt_prefixes")
                    if p.strip()
                ),
                footer_patterns=_as_strict_list_tuple(
                    raw_spec.get("footer_patterns"), f"agents.{alias}.footer_patterns"
                ),
                default_model=str(raw_spec.get("default_model") or "").strip(),
            )
        )
    registry = AgentRegistry(extra_specs=custom_specs)

    default_agent = (
        str(merged_env.get("HERDR_DEFAULT_AGENT") or bridge_tbl.get("default_agent") or "claude").strip().lower()
        or "claude"
    )
    if not registry.is_supported(default_agent):
        raise ValueError(
            f"Invalid default_agent={default_agent!r} (expected one of {', '.join(registry.allowed_names)})"
        )

    toml_roots = bridge_tbl.get("spawn_roots")
    env_roots = merged_env.get("HERDR_SPAWN_ROOTS", "").strip()
    if env_roots:
        spawn_roots = _default_spawn_roots_from_env(env_roots)
    elif toml_roots:
        parsed_roots = _as_str_tuple(toml_roots, "bridge.spawn_roots")
        spawn_roots = tuple(Path(p).expanduser() for p in parsed_roots)
    else:
        spawn_roots = _default_spawn_roots_from_env("")

    # 3. Bot channels ([bots.<name>] + TELEGRAM_BOT_TOKEN / TELEGRAM_PROD_BOT_TOKEN)
    primary_token = merged_env.get("TELEGRAM_BOT_TOKEN", "").strip()
    primary_chat_id = merged_env.get("TELEGRAM_CHAT_ID", "").strip()
    bots: dict[str, BotChannelSpec] = {}
    if primary_token:
        bots["default"] = BotChannelSpec(
            name="default",
            token=primary_token,
            chat_id=primary_chat_id,
            title="Herdr Telegram Bridge",
        )

    prod_token = merged_env.get("TELEGRAM_PROD_BOT_TOKEN", "").strip()
    prod_chat_id = merged_env.get("TELEGRAM_PROD_CHAT_ID", "").strip() or primary_chat_id
    if prod_token and prod_token != primary_token:
        bots["prod"] = BotChannelSpec(
            name="prod",
            token=prod_token,
            chat_id=prod_chat_id,
            title="Herdr Prod Support Bridge",
        )

    bots_tbl = toml_data.get("bots") or {}
    if not isinstance(bots_tbl, dict):
        raise ValueError("[bots] in config.toml must be a table")
    for raw_name, raw_bot in bots_tbl.items():
        if not isinstance(raw_bot, dict):
            raise ValueError(f"[bots.{raw_name}] must be a table")
        b_name = "default" if str(raw_name).strip().lower() in ("default", "main") else str(raw_name).strip().lower()
        if not BOT_NAME_RE.match(b_name):
            raise ValueError(f"Invalid bot channel name {raw_name!r}")
        token_env_key = str(raw_bot.get("token_env") or "").strip()
        chat_env_key = str(raw_bot.get("chat_id_env") or "").strip()
        tok = (merged_env.get(token_env_key, "") if token_env_key else str(raw_bot.get("token") or "")).strip()
        cid = (
            merged_env.get(chat_env_key, "") if chat_env_key else str(raw_bot.get("chat_id") or "")
        ).strip() or primary_chat_id
        title = str(raw_bot.get("title") or "").strip()
        if not tok and b_name in bots:
            tok = bots[b_name].token
            cid = cid or bots[b_name].chat_id
        if not tok:
            continue
        if b_name != "default" and "default" in bots and tok == bots["default"].token:
            continue
        bots[b_name] = BotChannelSpec(name=b_name, token=tok, chat_id=cid, title=title)

    # 4. [defaults] + [[routes]]
    defaults_tbl = toml_data.get("defaults") or {}
    if not isinstance(defaults_tbl, dict):
        raise ValueError("[defaults] in config.toml must be a table")

    idle_delay = float(
        merged_env.get("HERDR_IDLE_DELAY_SECONDS")
        if "HERDR_IDLE_DELAY_SECONDS" in merged_env and merged_env["HERDR_IDLE_DELAY_SECONDS"] != ""
        else defaults_tbl.get("idle_delay_seconds", 90.0)
    )
    blocked_delay = float(
        merged_env.get("HERDR_BLOCKED_DELAY_SECONDS")
        if "HERDR_BLOCKED_DELAY_SECONDS" in merged_env and merged_env["HERDR_BLOCKED_DELAY_SECONDS"] != ""
        else defaults_tbl.get("blocked_delay_seconds", 30.0)
    )
    if idle_delay < 0 or blocked_delay < 0:
        raise ValueError("idle_delay_seconds and blocked_delay_seconds must be non-negative")

    include_output = _as_bool(
        merged_env.get("HERDR_INCLUDE_OUTPUT")
        if "HERDR_INCLUDE_OUTPUT" in merged_env and merged_env["HERDR_INCLUDE_OUTPUT"] != ""
        else defaults_tbl.get("include_output"),
        True,
    )
    def_prov = (
        str(merged_env.get("HERDR_SUMMARY_PROVIDER") or defaults_tbl.get("summary_provider") or "auto").strip()
        or "auto"
    )
    def_model = str(merged_env.get("HERDR_SUMMARY_MODEL") or defaults_tbl.get("summary_model") or "").strip()
    def_bot = str(defaults_tbl.get("bot") or "default").strip().lower()
    if def_bot == "main":
        def_bot = "default"

    route_defaults = RouteDefaults(
        bot=def_bot,
        idle_delay_seconds=idle_delay,
        blocked_delay_seconds=blocked_delay,
        fast_track_replies=_as_bool(defaults_tbl.get("fast_track_replies"), True),
        silent_when_green=_as_bool(defaults_tbl.get("silent_when_green"), False),
        hold_on_short_timers=_as_bool(defaults_tbl.get("hold_on_short_timers"), True),
        suppress_support_done=_as_bool(defaults_tbl.get("suppress_support_done"), True),
        include_output=include_output,
        summary_provider=def_prov,
        summary_model=def_model,
        summary_style=str(defaults_tbl.get("summary_style") or "").strip(),
    )

    raw_routes = toml_data.get("routes")
    routes: list[RouteRule] = []
    if raw_routes is not None:
        if not isinstance(raw_routes, list):
            raise ValueError("[[routes]] in config.toml must be an array of tables")
        for idx, r_tbl in enumerate(raw_routes):
            if not isinstance(r_tbl, dict):
                raise ValueError(f"routes[{idx}] must be a table")
            r_name = str(r_tbl.get("name") or f"route-{idx + 1}").strip()
            r_bot_raw = r_tbl.get("bot")
            r_bot = str(r_bot_raw).strip().lower() if r_bot_raw is not None else None
            if r_bot == "main":
                r_bot = "default"
            routes.append(
                RouteRule(
                    name=r_name,
                    match_workspaces=_as_str_tuple(r_tbl.get("match_workspaces"), f"routes[{idx}].match_workspaces"),
                    match_agents=_as_str_tuple(r_tbl.get("match_agents"), f"routes[{idx}].match_agents"),
                    match_names=_as_str_tuple(r_tbl.get("match_names"), f"routes[{idx}].match_names"),
                    match_titles=_as_str_tuple(r_tbl.get("match_titles"), f"routes[{idx}].match_titles"),
                    bot=r_bot,
                    idle_delay_seconds=_as_opt_nonneg_float(
                        r_tbl.get("idle_delay_seconds"), f"routes[{idx}].idle_delay_seconds"
                    ),
                    blocked_delay_seconds=_as_opt_nonneg_float(
                        r_tbl.get("blocked_delay_seconds"), f"routes[{idx}].blocked_delay_seconds"
                    ),
                    fast_track_replies=_as_opt_bool(r_tbl.get("fast_track_replies")),
                    silent_when_green=_as_opt_bool(r_tbl.get("silent_when_green")),
                    hold_on_short_timers=_as_opt_bool(r_tbl.get("hold_on_short_timers")),
                    suppress_support_done=_as_opt_bool(r_tbl.get("suppress_support_done")),
                    include_output=_as_opt_bool(r_tbl.get("include_output")),
                    summary_provider=(
                        str(r_tbl["summary_provider"]).strip() if r_tbl.get("summary_provider") is not None else None
                    ),
                    summary_model=(
                        str(r_tbl["summary_model"]).strip() if r_tbl.get("summary_model") is not None else None
                    ),
                    summary_style=str(r_tbl.get("summary_style") or "").strip(),
                )
            )
    else:
        # Legacy .env compatibility: synthesize the 'prod' route when no config.toml exists (or when prod env vars are set)
        raw_prod_ws = merged_env.get("HERDR_PROD_WORKSPACES", "").strip()
        if not resolved_toml_path.exists() or "prod" in bots or raw_prod_ws:
            ws_patterns = _as_str_tuple(raw_prod_ws, "HERDR_PROD_WORKSPACES") if raw_prod_ws else LEGACY_PROD_WORKSPACES
            silent_green = _as_bool(merged_env.get("HERDR_SILENT_PROD_GREEN"), "prod" in bots)
            routes.append(
                RouteRule(
                    name="prod",
                    match_workspaces=ws_patterns,
                    bot="prod" if "prod" in bots else "default",
                    idle_delay_seconds=0.0,
                    silent_when_green=silent_green,
                    hold_on_short_timers=True,
                )
            )

    return BridgeConfig(
        bots=bots,
        rule_engine=RuleEngine(defaults=route_defaults, routes=routes),
        agent_registry=registry,
        poll_interval=poll_interval,
        read_lines=read_lines,
        summary_read_lines=summary_read_lines,
        summary_min_chars=summary_min_chars,
        default_agent=default_agent,
        spawn_roots=spawn_roots,
        spawn_timeout_ms=spawn_timeout_ms,
        summary_api_key=merged_env.get("HERDR_SUMMARY_API_KEY", "").strip(),
        summary_api_url=merged_env.get("HERDR_SUMMARY_API_URL", "").strip(),
    )
