#!/usr/bin/env python3
"""Send a Telegram message or photo from a Herdr agent and bind swipe-replies to the caller pane."""

from __future__ import annotations

import argparse
import dataclasses
import html
import os
import socket
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bridge import __version__
from herdr_telegram import (
    TELEGRAM_CAPTION_MAX_CHARS,
    AgentSnapshot,
    HerdrClient,
    MediaValidationError,
    PreparedOutboundMedia,
    TelegramClient,
    assign_routed_model_handles,
    default_config_toml_path,
    default_env_path,
    detect_pane_model,
    format_proactive_message,
    load_bridge_config,
    parse_env_file,
    read_outbound_image,
    record_proactive_target,
    redact_secrets,
    resolve_canonical_pane_id,
    strip_em_dashes,
)


def load_env(path: Path) -> dict[str, str]:
    return parse_env_file(path)


def run_cli(
    argv: list[str] | None = None,
    *,
    env_path: Path | None = None,
    config_path: Path | None = None,
    herdr: HerdrClient | None = None,
    client_factory: Callable[[str], TelegramClient] = TelegramClient,
    state_dir: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="telegram-send",
        description="Send a Telegram message to the user and bind replies to the calling Herdr pane.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--pane",
        default=os.environ.get("HERDR_PANE_ID", "").strip(),
        help="Herdr pane ID to bind replies to (defaults to $HERDR_PANE_ID)",
    )
    parser.add_argument(
        "--bot",
        default="auto",
        help="Bot channel name from config.toml/env, or 'auto' to route via rules (default: auto)",
    )
    parser.add_argument(
        "--prod",
        action="store_const",
        const="prod",
        dest="bot",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--photo",
        type=Path,
        default=None,
        help="Send a local image file (JPEG, PNG, WebP, or GIF)",
    )
    parser.add_argument("message", nargs="*", help="Message or caption text to send")
    args = parser.parse_args(argv)

    raw_msg = " ".join(args.message)
    if args.photo is None and not raw_msg.strip():
        parser.error("message is required unless --photo is provided")

    prepared: PreparedOutboundMedia | None = None
    if args.photo is not None:
        try:
            prepared = read_outbound_image(args.photo)
        except MediaValidationError as exc:
            print(f"Error: {redact_secrets(str(exc))}", file=sys.stderr)
            return 1

    cfg_env_path = env_path if env_path is not None else default_env_path()
    cfg_toml_path = (
        config_path
        if config_path is not None
        else (cfg_env_path.parent / "config.toml" if env_path is not None else default_config_toml_path())
    )
    raw_env = load_env(cfg_env_path)
    try:
        cfg = load_bridge_config(config_path=cfg_toml_path, env_path=cfg_env_path, environ={})
    except Exception as exc:
        print(f"Error loading herdr-telegram configuration: {exc}", file=sys.stderr)
        return 1

    requested_bot = args.bot.strip().lower() or "auto"
    if requested_bot == "main":
        requested_bot = "default"

    if requested_bot != "auto":
        if requested_bot in cfg.bots and cfg.bots[requested_bot].token and cfg.bots[requested_bot].chat_id:
            pass
        elif (
            requested_bot == "prod"
            and raw_env.get("TELEGRAM_PROD_BOT_TOKEN", "").strip()
            and "default" in cfg.bots
            and cfg.bots["default"].chat_id
        ):
            requested_bot = "default"
        else:
            missing_hint = "TELEGRAM_PROD_BOT_TOKEN" if requested_bot == "prod" else f"[bots.{requested_bot}] token"
            print(
                f"Error: bot channel '{requested_bot}' is missing {missing_hint} or chat_id in {cfg_env_path} / {cfg_toml_path}",
                file=sys.stderr,
            )
            return 1
    else:
        default_spec = cfg.bots.get("default")
        if default_spec is None or not default_spec.token or not default_spec.chat_id:
            print(
                f"Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured in {cfg_env_path}",
                file=sys.stderr,
            )
            return 1

    raw_pane = " ".join(args.pane.split())
    herdr_client = herdr if herdr is not None else HerdrClient(agent_registry=cfg.agent_registry)
    pane_id = herdr_client.resolve_pane_id(raw_pane) if raw_pane else ""
    host_label = html.escape(strip_em_dashes(socket.gethostname() or "host"))
    icon = "📷" if prepared is not None else "💬"
    header = f"{icon} <code>{html.escape(pane_id)}</code>" if pane_id else f"{icon} <b>{host_label}</b>"
    caller_snap: AgentSnapshot | None = None
    if pane_id:
        try:
            snapshots = herdr_client.list_agents()
            canonical = resolve_canonical_pane_id(pane_id, snapshots) or pane_id
            pane_id = canonical
            if canonical in snapshots:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    footers = dict(
                        zip(
                            snapshots.keys(),
                            pool.map(herdr_client.peek_pane_footer, snapshots.keys()),
                            strict=True,
                        )
                    )
                model_by_pane = {
                    pid: detect_pane_model(
                        footers.get(pid, ""),
                        cfg.agent_registry.infer_alias(s.agent, s.name),
                        agent_registry=cfg.agent_registry,
                    )
                    for pid, s in snapshots.items()
                }
                alias_snapshots = {
                    pid: (
                        dataclasses.replace(s, agent=eff)
                        if (eff := cfg.agent_registry.infer_alias(s.agent, s.name)) != s.agent
                        else s
                    )
                    for pid, s in snapshots.items()
                }
                snapshots = assign_routed_model_handles(
                    alias_snapshots,
                    model_by_pane,
                    rule_engine=cfg.rule_engine,
                    available_bots=cfg.bots.keys(),
                )
                caller_snap = snapshots[canonical]
                header = f"{icon} <b>{html.escape(strip_em_dashes(caller_snap.header_label))}</b>"
            else:
                header = f"{icon} <code>{html.escape(canonical)}</code>"
        except Exception:
            pass

    if requested_bot != "auto":
        bot_role = requested_bot
    elif caller_snap is not None:
        bot_role = cfg.rule_engine.resolve(caller_snap, available_bots=cfg.bots.keys()).bot
    else:
        def_bot = cfg.rule_engine.defaults.bot
        bot_role = def_bot if def_bot in cfg.bots else "default"

    chosen_spec = cfg.bots.get(bot_role) or cfg.bots.get("default")
    if chosen_spec is None or not chosen_spec.token or not chosen_spec.chat_id:
        print(f"Error: no valid bot credentials found for channel '{bot_role}'", file=sys.stderr)
        return 1

    client = client_factory(chosen_spec.token)
    try:
        if prepared is not None:
            caption_html = format_proactive_message(header, raw_msg) if raw_msg.strip() else header
            if len(caption_html) <= TELEGRAM_CAPTION_MAX_CHARS:
                msg_id = client.send_media(chosen_spec.chat_id, prepared, caption=caption_html)
                if pane_id:
                    record_proactive_target(msg_id, pane_id, state_dir=state_dir, bot_role=bot_role)
                print(
                    f"Sent Telegram {prepared.telegram_field} "
                    f"(message_id={msg_id}, bot={bot_role}, pane={pane_id or 'none'})"
                )
            else:
                photo_msg_id = client.send_media(chosen_spec.chat_id, prepared, caption=header)
                if pane_id:
                    record_proactive_target(photo_msg_id, pane_id, state_dir=state_dir, bot_role=bot_role)
                text_msg_id = client.send_message(
                    chosen_spec.chat_id,
                    caption_html,
                    reply_to_message_id=photo_msg_id,
                )
                if pane_id:
                    record_proactive_target(text_msg_id, pane_id, state_dir=state_dir, bot_role=bot_role)
                print(
                    f"Sent Telegram {prepared.telegram_field} + caption reply "
                    f"(message_id={photo_msg_id},{text_msg_id}, bot={bot_role}, pane={pane_id or 'none'})"
                )
            return 0

        full_text = format_proactive_message(header, raw_msg)
        msg_id = client.send_message(chosen_spec.chat_id, full_text)
        if pane_id:
            record_proactive_target(msg_id, pane_id, state_dir=state_dir, bot_role=bot_role)
        print(f"Sent Telegram message (message_id={msg_id}, bot={bot_role}, pane={pane_id or 'none'})")
        return 0
    except (MediaValidationError, RuntimeError, OSError) as exc:
        print(f"Error sending Telegram message: {redact_secrets(str(exc))}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
