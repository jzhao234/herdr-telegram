from __future__ import annotations

import contextlib
import dataclasses
import importlib.machinery
import importlib.util
import io
import json
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bridge import (
    HEIC_HINT_MESSAGE,
    HERDR_BUILTIN_AGENT_KINDS,
    AgentRegistry,
    AgentSpec,
    MediaValidationError,
    RouteDefaults,
    RouteRule,
    RuleEngine,
    load_bridge_config,
    match_glob_token,
    parse_env_file,
    prune_media_dir,
    read_outbound_image,
    run_doctor,
    run_setup,
    save_inbound_image,
    seed_environ_from_env_file,
    sniff_image_bytes,
    upsert_env_values,
)
from herdr_telegram import (
    AGENT_NAME_RE,
    BOT_MENU_COMMANDS,
    DEFAULT_SUMMARY_MODEL,
    DEFAULT_SUMMARY_PROVIDER,
    SUMMARY_DENY_SETTINGS_JSON,
    AgentSnapshot,
    AgentStartResult,
    AgyFlashSummarizer,
    BridgeDaemon,
    BridgeState,
    CreatedWorkspace,
    DigestSections,
    HerdrClient,
    TelegramClient,
    TurnSummarizer,
    VoiceTranscriber,
    _render_digest_sections_html,
    assign_channel_model_handles,
    assign_model_handles,
    assign_space_command_slugs,
    build_dynamic_bot_commands,
    build_spawn_agent_name,
    build_transition_keyboard,
    classify_transition,
    clean_terminal_output,
    command_slug,
    detect_mid_workflow_hold_seconds,
    detect_pane_model,
    discover_spawn_repos,
    extract_blocked_menu,
    extract_last_agent_turn,
    extract_spawn_picker_agent,
    extract_spoken_space_prefix,
    format_display_handle,
    format_done_message,
    format_proactive_message,
    format_space_label,
    format_status_overview,
    format_transition_message,
    inspect_inbound_voice_message,
    is_cowork_support,
    is_prod_snapshot,
    is_prod_workspace,
    ordered_space_snapshots,
    parse_digest_json,
    parse_prod_workspaces,
    parse_slash_command,
    parse_spawn_args,
    record_proactive_target,
    redact_secrets,
    resolve_reply_target,
    resolve_spawn_repo,
    resolve_summary_backend,
    resolve_voice_backend,
    sniff_audio_bytes,
    term_tag,
    workspace_lead_and_reviews,
)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_snap(
    pane_id: str = "w15:p1",
    workspace_id: str = "w15",
    workspace_label: str = "Migration",
    agent: str = "claude",
    status: str = "idle",
    name: str | None = None,
    title: str = "Auth plugin",
    terminal_id: str = "term_1",
    state_change_seq: int = 42,
    cwd: str = "",
) -> AgentSnapshot:
    return AgentSnapshot(
        pane_id=pane_id,
        workspace_id=workspace_id,
        workspace_label=workspace_label,
        agent=agent,
        status=status,
        name=name,
        title=title,
        terminal_id=terminal_id,
        state_change_seq=state_change_seq,
        cwd=cwd,
    )


def make_png_bytes(width: int = 16, height: int = 12) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


def make_gif_bytes(width: int = 16, height: int = 12) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


class FakeHerdr:
    def __init__(self, snapshots: dict[str, AgentSnapshot]) -> None:
        self.snapshots = snapshots
        self.prompts: list[tuple[str, str]] = []
        self.keys: list[tuple[str, list[str]]] = []
        self.read_calls: list[tuple[str, int, str]] = []
        self.footer_peeks: list[tuple[str, int]] = []
        self.outputs: dict[str, str] = {}
        self.visible_outputs: dict[str, str] = {}
        self.footer_outputs: dict[str, str] = {}
        self.created_workspaces: list[tuple[Path, str]] = []
        self.closed_workspaces: list[str] = []
        self.started_agents: list[tuple[str, str, str, int]] = []
        self.started_extra_args: dict[str, tuple[str, ...]] = {}
        self.start_result = AgentStartResult(outcome="ready")
        self.spawn_post_start_status = "idle"
        self.spawn_post_start_seq = 1
        self.prompt_ok = True
        self.prompt_err = ""
        self.prompt_detail_status: str | None = None
        self.spawn_post_prompt_status: str | None = None
        self.spawn_post_prompt_seq: int | None = None
        self.send_keys_status = "sent"
        self.send_keys_err = ""
        self.fail_list_agents = False

    def list_agents(self) -> dict[str, AgentSnapshot]:
        if self.fail_list_agents:
            raise RuntimeError("herdr daemon unavailable")
        return self.snapshots

    def create_workspace(self, cwd: Path, label: str) -> CreatedWorkspace:
        self.created_workspaces.append((cwd, label))
        return CreatedWorkspace(
            workspace_id="w23",
            label=label,
            pane_id="w23:p1",
            terminal_id="term_23",
        )

    def close_workspace(self, workspace_id: str) -> tuple[bool, str]:
        self.closed_workspaces.append(workspace_id)
        return True, ""

    def start_agent(
        self,
        name: str,
        kind: str,
        pane_id: str,
        timeout_ms: int = 30000,
        *,
        extra_args: Sequence[str] | None = None,
    ) -> AgentStartResult:
        self.started_agents.append((name, kind, pane_id, timeout_ms))
        self.started_extra_args[pane_id] = tuple(extra_args or ())
        if self.start_result.outcome != "failed":
            self.snapshots[pane_id] = AgentSnapshot(
                pane_id=pane_id,
                workspace_id="w23",
                workspace_label=self.created_workspaces[-1][1] if self.created_workspaces else "w23",
                agent=kind,
                status=self.spawn_post_start_status,
                name=name,
                title="",
                terminal_id="term_23",
                state_change_seq=self.spawn_post_start_seq,
                cwd=str(self.created_workspaces[-1][0]) if self.created_workspaces else "",
            )
        return self.start_result

    def read_agent(self, target: str, lines: int = 120) -> str:
        self.read_calls.append((target, lines, "recent-unwrapped"))
        if target in self.outputs:
            return self.outputs[target]
        return f"output from {target} ({lines})"

    def read_pane_visible(self, target: str, lines: int = 40) -> str:
        self.read_calls.append((target, lines, "visible"))
        if target in self.visible_outputs:
            return self.visible_outputs[target]
        if target in self.outputs:
            return self.outputs[target]
        return f"visible output from {target} ({lines})"

    def peek_pane_footer(self, target: str, lines: int = 10) -> str:
        self.footer_peeks.append((target, lines))
        return self.footer_outputs.get(target, "")

    def resolve_pane_id(self, raw_pane: str = "") -> str:
        return raw_pane.strip()

    def prompt_agent(self, target: str, prompt: str) -> tuple[bool, str]:
        self.prompts.append((target, prompt))
        if target in self.snapshots and self.spawn_post_prompt_status is not None:
            prev = self.snapshots[target]
            self.snapshots[target] = AgentSnapshot(
                pane_id=prev.pane_id,
                workspace_id=prev.workspace_id,
                workspace_label=prev.workspace_label,
                agent=prev.agent,
                status=self.spawn_post_prompt_status,
                name=prev.name,
                title=prev.title,
                terminal_id=prev.terminal_id,
                state_change_seq=(
                    self.spawn_post_prompt_seq if self.spawn_post_prompt_seq is not None else prev.state_change_seq
                ),
                cwd=prev.cwd,
            )
        return self.prompt_ok, self.prompt_err

    def prompt_agent_detailed(self, target: str, prompt: str) -> tuple[str, str]:
        ok, err = self.prompt_agent(target, prompt)
        if self.prompt_detail_status is not None:
            return self.prompt_detail_status, err
        return ("sent" if ok else "rejected"), err

    def send_keys_detailed(self, target: str, keys: list[str]) -> tuple[str, str]:
        self.keys.append((target, keys))
        return self.send_keys_status, self.send_keys_err

    def send_keys(self, target: str, keys: list[str]) -> tuple[bool, str]:
        status, err = self.send_keys_detailed(target, keys)
        return status == "sent", err


class FakeTelegram:
    def __init__(self, fail_on_call: int | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.sent_media: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.download_calls: list[str] = []
        self.callbacks: list[tuple[str, str]] = []
        self.edited: list[dict[str, Any]] = []
        self.synced_commands: list[tuple[tuple[tuple[str, str], ...], dict[str, Any] | None]] = []
        self.next_id = 100
        self.calls = 0
        self.fail_on_call = fail_on_call

    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool = False,
    ) -> int:
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("Simulated Telegram HTTP failure")
        self.next_id += 1
        self.sent.append(
            {
                "message_id": self.next_id,
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "reply_to_message_id": reply_to_message_id,
                "disable_notification": disable_notification,
            }
        )
        return self.next_id

    def send_media(
        self,
        chat_id: str,
        prepared: Any,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool = False,
        **_: Any,
    ) -> int:
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("Simulated Telegram media HTTP failure")
        self.next_id += 1
        self.sent_media.append(
            {
                "message_id": self.next_id,
                "chat_id": chat_id,
                "method": prepared.telegram_method,
                "field": prepared.telegram_field,
                "filename": prepared.filename,
                "mime_type": prepared.metadata.mime_type,
                "size": len(prepared.data),
                "caption": caption,
                "reply_markup": reply_markup,
                "reply_to_message_id": reply_to_message_id,
                "disable_notification": disable_notification,
            }
        )
        return self.next_id

    def download_file(self, file_id: str, **_: Any) -> bytes:
        self.download_calls.append(file_id)
        if file_id not in self.files:
            raise RuntimeError(f"Telegram getFile HTTP 400 (missing {file_id})")
        return self.files[file_id]

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self.callbacks.append((callback_query_id, text))

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        self.edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return True

    def set_my_commands(
        self,
        commands: Any = (),
        *,
        scope: dict[str, Any] | None = None,
    ) -> None:
        self.synced_commands.append((tuple(commands), scope))


class HerdrTelegramBridgeTest(unittest.TestCase):
    def test_classify_transition_including_back_to_back_blocked(self) -> None:
        self.assertIsNone(classify_transition(None, "idle", 1, is_initial_poll=True))
        self.assertEqual(classify_transition(None, "blocked", 1, is_initial_poll=True), "blocked")
        self.assertIsNone(classify_transition(("blocked", 10), "blocked", 10))
        # Back-to-back blocked with new state_change_seq triggers a new blocked alert
        self.assertEqual(classify_transition(("blocked", 10), "blocked", 11), "blocked")
        self.assertEqual(classify_transition(("working", 1), "done", 2), "done")
        self.assertEqual(classify_transition(("working", 1), "idle", 2), "done")
        self.assertEqual(classify_transition(("idle", 1), "done", 2), "done")
        self.assertIsNone(classify_transition(("done", 2), "idle", 3))

    def test_herdr_client_cli_argument_order_matches_0_8_2(self) -> None:
        client = HerdrClient(binary="/usr/bin/herdr")
        recorded: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            recorded.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            client.read_agent("w15:p1", lines=20)
            client.prompt_agent("w15:p1", "- bullet item")
            client.send_keys("w15:p1", ["Enter", "1"])

        self.assertEqual(
            recorded[0],
            ["/usr/bin/herdr", "agent", "read", "w15:p1", "--source", "recent-unwrapped", "--lines", "20"],
        )
        self.assertEqual(
            recorded[1],
            [
                "/usr/bin/herdr",
                "agent",
                "prompt",
                "w15:p1",
                " - bullet item",
                "--wait",
                "--until",
                "working",
                "--until",
                "blocked",
                "--until",
                "done",
                "--timeout",
                "6000",
            ],
        )
        self.assertEqual(
            recorded[2],
            ["/usr/bin/herdr", "agent", "send-keys", "w15:p1", "enter", "1"],
        )

    def test_redact_secrets_and_clean_output(self) -> None:
        raw = (
            "API_KEY=supersecret123 sk-abcdefghijklmnopqrstuvwxyz123456 "
            "postgres://admin:dbpass99@localhost/db "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2 "
            '"client_secret": "topsecretval"'
        )
        redacted = redact_secrets(raw)
        self.assertNotIn("supersecret123", redacted)
        self.assertNotIn("sk-abcdef", redacted)
        self.assertNotIn("dbpass99", redacted)
        self.assertNotIn("eyJhbGci", redacted)
        self.assertNotIn("topsecretval", redacted)
        self.assertEqual(clean_terminal_output("\n\n  hello \n\n"), "  hello")

    def test_format_transition_message_escapes_html(self) -> None:
        snap = make_snap(workspace_label="Fix <script> & stuff", title="Title <img src=x>")
        msg = format_transition_message("done", snap, "line 1 <tag> & more")
        self.assertIn("✅ <b>Done</b>", msg)
        self.assertIn("Fix &lt;script&gt; &amp; stuff (🤖 Claude)", msg)
        self.assertNotIn("Title", msg)
        self.assertIn("line 1 &lt;tag&gt; &amp; more", msg)

        blocked_msg = format_transition_message("blocked", snap, "Approve <cmd>?")
        self.assertIn("Title &lt;img src=x&gt;", blocked_msg)
        self.assertIn("Approve &lt;cmd&gt;?", blocked_msg)

    def test_resolve_reply_target_priority_and_canonical_resolution(self) -> None:
        snap1 = make_snap(pane_id="w15:p1", workspace_label="Migration", name="reviewer")
        snap2 = make_snap(pane_id="w16:p2", workspace_label="note")
        snapshots = {"w15:p1": snap1, "w16:p2": snap2}
        state = BridgeState(pinned_target="w16:p2", message_targets={"42": "w15:p1"})

        target, body = resolve_reply_target("w15:p1: run pytest", None, state, snapshots)
        self.assertEqual(target, "w15:p1")
        self.assertEqual(body, "run pytest")

        target, body = resolve_reply_target(
            "note: do not touch prod",
            {"message_id": 42, "text": "irrelevant"},
            state,
            snapshots,
        )
        self.assertEqual(target, "w15:p1")
        self.assertEqual(body, "note: do not touch prod")

        # Agent name prefix resolves to canonical pane_id
        target, body = resolve_reply_target("reviewer: check diff", None, state, snapshots)
        self.assertEqual(target, "w15:p1")
        self.assertEqual(body, "check diff")

    def test_transient_alert_failure_retries_next_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr({"w15:p1": make_snap(pane_id="w15:p1", status="working")})
            tg = FakeTelegram(fail_on_call=1)
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
            )
            daemon.poll_herdr_once()

            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="done")
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 0)

            clock.advance(90.0)
            # First delivery attempt at t=90s fails transiently
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 0)

            # Next poll retries and succeeds without waiting another 90s
            clock.advance(2.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 1)

    def test_approval_button_consumed_once_and_rejects_seq_zero_or_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = FakeHerdr({"w15:p1": make_snap(pane_id="w15:p1", status="blocked", state_change_seq=10)})
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
            )
            snapshots = daemon.poll_herdr_once()

            # seq=0 is always rejected
            daemon.handle_update(
                {
                    "update_id": 1,
                    "callback_query": {
                        "id": "cq0",
                        "from": {"id": 777},
                        "message": {"chat": {"id": 777, "type": "private"}},
                        "data": "key:w15:p1:0:y",
                    },
                },
                snapshots,
            )
            self.assertEqual(herdr.keys, [])

            # Valid seq=10 fires once (single key 'y', no extra enter)
            click = {
                "update_id": 2,
                "callback_query": {
                    "id": "cq1",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 777, "type": "private"}},
                    "data": "key:w15:p1:10:y",
                },
            }
            daemon.handle_update(click, snapshots)
            self.assertEqual(herdr.keys, [("w15:p1", ["y"])])

            # Second click with the same (pane, seq) is rejected as already consumed
            daemon.handle_update(click, snapshots)
            self.assertEqual(len(herdr.keys), 1)

    def test_group_chat_and_stale_messages_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = FakeHerdr({"w15:p1": make_snap(status="idle")})
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
            )
            snapshots = daemon.poll_herdr_once()

            # Group chat rejected even if chat.id matches
            daemon.handle_update(
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 10,
                        "from": {"id": 777},
                        "chat": {"id": 777, "type": "group"},
                        "text": "w15:p1: hack",
                    },
                },
                snapshots,
            )
            self.assertEqual(herdr.prompts, [])

            # Stale message (>300s old) rejected without prompting
            daemon.handle_update(
                {
                    "update_id": 2,
                    "message": {
                        "message_id": 11,
                        "date": int(time.time()) - 1000,
                        "from": {"id": 777},
                        "chat": {"id": 777, "type": "private"},
                        "text": "w15:p1: old command",
                    },
                },
                snapshots,
            )
            self.assertEqual(herdr.prompts, [])
            self.assertIn("Ignored stale message", tg.sent[-1]["text"])

    def test_update_not_replayed_when_confirmation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = FakeHerdr({"w15:p1": make_snap(status="idle")})
            tg = FakeTelegram(fail_on_call=1)
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
            )
            snapshots = daemon.poll_herdr_once()
            upd = {
                "update_id": 55,
                "message": {
                    "message_id": 11,
                    "from": {"id": 777},
                    "chat": {"id": 777, "type": "private"},
                    "text": "w15:p1: deploy once",
                },
            }
            daemon.process_updates([upd], snapshots)
            daemon.process_updates([upd], snapshots)
            self.assertEqual(herdr.prompts, [("w15:p1", "deploy once")])

    def test_parse_slash_command_and_spawn_args_preserve_verbatim_prompt(self) -> None:
        self.assertEqual(
            parse_slash_command("/spawn@my_bot repo fix 'quotes'\nand \"lines\""),
            ("spawn", "repo fix 'quotes'\nand \"lines\""),
        )
        self.assertEqual(parse_slash_command("/keyboard something"), ("keyboard", "something"))
        self.assertIsNone(parse_slash_command("w15:p1: /status"))

        agent, repo, prompt = parse_spawn_args("knowledge-base")
        self.assertEqual((agent, repo, prompt), ("claude", "knowledge-base", ""))

        agent, repo, prompt = parse_spawn_args("--agent codex work/fleet fix 'bug' & \"quote\"\n- bullet")
        self.assertEqual(agent, "codex")
        self.assertEqual(repo, "work/fleet")
        self.assertEqual(prompt, "fix 'bug' & \"quote\"\n- bullet")

        agent, repo, prompt = parse_spawn_args("-a agy Personal/brain")
        self.assertEqual((agent, repo, prompt), ("agy", "Personal/brain", ""))

        agent, repo, prompt = parse_spawn_args("--agent=codex brain - leading dash")
        self.assertEqual((agent, repo, prompt), ("codex", "brain", "- leading dash"))

        for bad in ("", "--agent", "--agent=codex", "--agent foo bar", "--unknown repo", "-x repo", "--agent= repo"):
            with self.assertRaises(ValueError):
                parse_spawn_args(bad)

    def test_resolve_spawn_repo_containment_and_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            work = base / "work"
            personal = base / "Personal"
            outside = base / "outside-secret"
            (work / "api-service").mkdir(parents=True)
            (work / "shared").mkdir(parents=True)
            (personal / "Shared").mkdir(parents=True)
            (personal / "notes-app").mkdir(parents=True)
            outside.mkdir(parents=True)
            (personal / "escape-link").symlink_to(outside)
            (personal / "inside-link").symlink_to(personal / "notes-app")
            roots = [work, personal]

            resolved, label = resolve_spawn_repo("api-service", roots)
            self.assertEqual((resolved, label), ((work / "api-service").resolve(), "api-service"))

            # Case-insensitive match when unambiguous
            resolved, label = resolve_spawn_repo("NOTES-APP", roots)
            self.assertEqual((resolved, label), ((personal / "notes-app").resolve(), "notes-app"))

            # Symlink staying inside root is allowed
            resolved, label = resolve_spawn_repo("inside-link", roots)
            self.assertEqual((resolved, label), ((personal / "notes-app").resolve(), "inside-link"))

            # Cross-root case-insensitive collision requires <root>/<repo>
            with self.assertRaisesRegex(ValueError, "Ambiguous repository"):
                resolve_spawn_repo("shared", roots)
            resolved, label = resolve_spawn_repo("Personal/shared", roots)
            self.assertEqual((resolved, label), ((personal / "Shared").resolve(), "Shared"))

            # Symlink escaping root is rejected
            with self.assertRaisesRegex(ValueError, "resolves outside allowed spawn roots"):
                resolve_spawn_repo("escape-link", roots)

            # Path traversal and hidden dirs rejected
            for bad in ("../outside-secret", "Personal/../outside-secret", "/etc", "~/projects", ".git", "a/b/c"):
                with self.assertRaises(ValueError):
                    resolve_spawn_repo(bad, roots)

    def test_build_spawn_agent_name_always_valid_and_unique_per_pane(self) -> None:
        for raw in ("2026-tax", "My.Repo_With_Very_Long_Name_1234567890", "---", "notes-app"):
            built = build_spawn_agent_name(raw, "w23:p1", "claude")
            self.assertRegex(built, AGENT_NAME_RE)
            self.assertTrue(built.endswith("w23-p1"))

    def test_herdr_client_spawn_timeouts_codex_args_and_json_error_before_redaction(self) -> None:
        client = HerdrClient(binary="/usr/bin/herdr")
        calls: list[tuple[list[str], float]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append((cmd, float(kwargs["timeout"])))
            if cmd[1:3] == ["workspace", "create"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='{"result":{"workspace":{"workspace_id":"w25","label":"brain"},"root_pane":{"pane_id":"w25:p1","terminal_id":"term_25"}}}',
                    stderr="",
                )
            if cmd[1:3] == ["pane", "current"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='{"result":{"pane":{"pane_id":"w25:p1"}}}',
                    stderr="",
                )
            if cmd[1:3] == ["agent", "start"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr='{"error":{"code":"agent_not_ready","message":"waiting on sk-abcdefghijklmnopqrstuvwx"}}',
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="{}", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            created = client.create_workspace(Path("/tmp/brain"), "brain")
            self.assertEqual((created.workspace_id, created.pane_id, created.terminal_id), ("w25", "w25:p1", "term_25"))
            self.assertEqual(client.resolve_pane_id(":p1"), "w25:p1")
            res = client.start_agent("brain-w25-p1", "codex", "w25:p1", timeout_ms=45000)

        self.assertEqual(res.outcome, "blocked")
        self.assertEqual(res.code, "agent_not_ready")
        self.assertIn("sk-[REDACTED]", res.error)
        start_cmd, start_timeout = calls[-1]
        self.assertEqual(
            start_cmd,
            [
                "/usr/bin/herdr",
                "agent",
                "start",
                "brain-w25-p1",
                "--kind",
                "codex",
                "--pane",
                "w25:p1",
                "--timeout",
                "45000",
                "--",
                "--no-alt-screen",
            ],
        )
        self.assertEqual(start_timeout, 55.0)

    def test_spawn_ready_without_and_with_fast_completing_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Personal"
            (root / "brain").mkdir(parents=True)
            herdr = FakeHerdr({})
            tg = FakeTelegram()
            clock = FakeClock()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                spawn_roots=[root],
                clock=clock,
            )
            snapshots = daemon.poll_herdr_once()

            # 1. Spawn without prompt: next poll (even 120s later) must NOT fire a false Done alert
            daemon.handle_update(
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 10,
                        "from": {"id": 777},
                        "chat": {"id": 777, "type": "private"},
                        "text": "/spawn brain",
                    },
                },
                snapshots,
            )
            self.assertEqual(len(tg.sent), 1)
            self.assertIn("Spawned", tg.sent[-1]["text"])
            clock.advance(120.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 1)

            # 2. Spawn with prompt that completes in <50ms (already 'done' after prompt_agent): fires Done after 90s!
            clock2 = FakeClock()
            herdr2 = FakeHerdr({})
            herdr2.spawn_post_prompt_status = "done"
            herdr2.spawn_post_prompt_seq = 2
            tg2 = FakeTelegram()
            daemon2 = BridgeDaemon(
                herdr=herdr2,  # type: ignore[arg-type]
                telegram=tg2,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state2.json",
                spawn_roots=[root],
                clock=clock2,
            )
            snapshots2 = daemon2.poll_herdr_once()
            daemon2.handle_update(
                {
                    "update_id": 2,
                    "message": {
                        "message_id": 11,
                        "from": {"id": 777},
                        "chat": {"id": 777, "type": "private"},
                        "text": "/spawn --agent codex brain check tasks",
                    },
                },
                snapshots2,
            )
            self.assertEqual(herdr2.prompts, [("w23:p1", "check tasks")])
            self.assertEqual(len(tg2.sent), 1)
            # Prompted from Telegram via /spawn -> skips the 90s wait and delivers Done immediately (once)
            daemon2.poll_herdr_once()
            self.assertEqual(len(tg2.sent), 2)
            self.assertIn("✅ <b>Done</b>", tg2.sent[-1]["text"])
            clock2.advance(90.0)
            daemon2.poll_herdr_once()
            self.assertEqual(len(tg2.sent), 2)

    def test_spawn_blocked_retries_on_telegram_failure_and_failed_closes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Personal"
            (root / "new-repo").mkdir(parents=True)

            # 1. Blocked startup where first Telegram send fails transiently -> delivers blocked alert after 30s
            clock = FakeClock()
            herdr = FakeHerdr({})
            herdr.start_result = AgentStartResult(outcome="blocked", code="agent_not_ready", error="agent_not_ready")
            herdr.spawn_post_start_status = "blocked"
            herdr.spawn_post_start_seq = 7
            tg = FakeTelegram(fail_on_call=1)
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                spawn_roots=[root],
                clock=clock,
            )
            snapshots = daemon.poll_herdr_once()
            daemon.process_updates(
                [
                    {
                        "update_id": 10,
                        "message": {
                            "message_id": 20,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "/spawn new-repo run tests",
                        },
                    }
                ],
                snapshots,
            )
            self.assertEqual(herdr.closed_workspaces, [])
            self.assertEqual(herdr.prompts, [])
            self.assertEqual(len(tg.sent), 0)

            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 0)
            clock.advance(30.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 1)
            self.assertIn("⚠️ <b>Blocked</b>", tg.sent[-1]["text"])
            self.assertIsNotNone(tg.sent[-1]["reply_markup"])

            # 2. Definite launch failure closes orphan workspace; ambiguous timeout leaves workspace open
            herdr_fail = FakeHerdr({})
            herdr_fail.start_result = AgentStartResult(
                outcome="failed", code="agent_start_failed", error="missing binary"
            )
            tg_fail = FakeTelegram()
            daemon_fail = BridgeDaemon(
                herdr=herdr_fail,  # type: ignore[arg-type]
                telegram=tg_fail,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state_fail.json",
                spawn_roots=[root],
            )
            daemon_fail.handle_update(
                {
                    "update_id": 11,
                    "message": {
                        "message_id": 21,
                        "from": {"id": 777},
                        "chat": {"id": 777, "type": "private"},
                        "text": "/spawn new-repo",
                    },
                },
                {},
            )
            self.assertEqual(herdr_fail.closed_workspaces, ["w23"])

            herdr_ambig = FakeHerdr({})
            herdr_ambig.start_result = AgentStartResult(outcome="ambiguous", code="timeout", error="timed out")
            herdr_ambig.spawn_post_start_status = "working"
            daemon_ambig = BridgeDaemon(
                herdr=herdr_ambig,  # type: ignore[arg-type]
                telegram=tg_fail,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state_ambig.json",
                spawn_roots=[root],
            )
            daemon_ambig.handle_update(
                {
                    "update_id": 12,
                    "message": {
                        "message_id": 22,
                        "from": {"id": 777},
                        "chat": {"id": 777, "type": "private"},
                        "text": "/spawn new-repo",
                    },
                },
                {},
            )
            self.assertEqual(herdr_ambig.closed_workspaces, [])

    def test_callback_fails_closed_when_list_agents_raises_and_slash_prefix_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = FakeHerdr({"w15:p1": make_snap(pane_id="w15:p1", status="blocked", state_change_seq=10)})
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
            )
            snapshots = daemon.poll_herdr_once()

            herdr.fail_list_agents = True
            click = {
                "update_id": 1,
                "callback_query": {
                    "id": "cq_err",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 777, "type": "private"}},
                    "data": "key:w15:p1:10:y",
                },
            }
            daemon.handle_update(click, snapshots)
            self.assertEqual(herdr.keys, [])
            self.assertIn("Could not verify live agent state", tg.callbacks[-1][1])

            # Once list_agents recovers, the unburned button succeeds
            herdr.fail_list_agents = False
            daemon.handle_update(click, snapshots)
            self.assertEqual(herdr.keys, [("w15:p1", ["y"])])

            # '/keyboard' is NOT treated as '/key'
            daemon.handle_update(
                {
                    "update_id": 2,
                    "message": {
                        "message_id": 30,
                        "from": {"id": 777},
                        "chat": {"id": 777, "type": "private"},
                        "text": "/keyboard layout check",
                    },
                },
                snapshots,
            )
            self.assertEqual(herdr.prompts, [("w15:p1", "/keyboard layout check")])

    def test_proactive_message_header_first_line_routing_and_length_bound(self) -> None:
        snap_sender = make_snap(pane_id="w22:p1", workspace_label="Lead", agent="agy")
        snap_other = make_snap(pane_id="w10:p1", workspace_label="Old")
        snapshots = {"w22:p1": snap_sender, "w10:p1": snap_other}
        state = BridgeState()

        # Reply to a telegram-send message whose workspace label ALSO contains a pane ID ("Handoff w10:p1")
        target, body = resolve_reply_target(
            "ship it",
            {
                "message_id": 999,
                "text": "💬 [w22:p1] Handoff w10:p1 (agy · w22:p1)\nMerged work from w10:p1 successfully.",
            },
            state,
            snapshots,
        )
        self.assertEqual(target, "w22:p1")
        self.assertEqual(body, "ship it")

        # Huge HTML-heavy payload stays safely below 4096 chars and redacts secrets
        rendered = format_proactive_message("💬 <b>w22:p1</b>", "sk-abcdefghijklmnopqrstuvwxyz123456 " + ("<" * 5000))
        self.assertLessEqual(len(rendered), 4000)
        self.assertNotIn("sk-abcdef", rendered)
        self.assertIn("…[truncated]", rendered)

    def test_unclassified_startup_error_and_uncertain_prompt_delivery_and_timeout_bounds(self) -> None:
        client = HerdrClient(binary="/usr/bin/herdr")
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["/usr/bin/herdr"],
                returncode=1,
                stdout="",
                stderr="socket disconnected mid-response",
            ),
        ):
            start_res = client.start_agent("brain-w25-p1", "claude", "w25:p1", timeout_ms=30000)
            prompt_status, _ = client.prompt_agent_detailed("w25:p1", "hello")
        self.assertEqual(start_res.outcome, "ambiguous")
        self.assertEqual(prompt_status, "ambiguous")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Personal"
            (root / "brain").mkdir(parents=True)

            # Out-of-range spawn_timeout_ms rejected
            for bad_ms in (2000, 3000, 120000):
                with self.assertRaises(ValueError):
                    BridgeDaemon(
                        herdr=FakeHerdr({}),  # type: ignore[arg-type]
                        telegram=FakeTelegram(),  # type: ignore[arg-type]
                        chat_id="777",
                        state_path=Path(tmp) / "s.json",
                        spawn_timeout_ms=bad_ms,
                    )

            # Uncertain prompt delivery warns against duplicate retry and still alerts on completion
            clock = FakeClock()
            herdr = FakeHerdr({})
            herdr.prompt_ok = False
            herdr.prompt_err = "herdr agent prompt timed out"
            herdr.prompt_detail_status = "ambiguous"
            herdr.spawn_post_prompt_status = "working"
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                spawn_roots=[root],
                clock=clock,
            )
            snapshots = daemon.poll_herdr_once()
            daemon.handle_update(
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 10,
                        "from": {"id": 777},
                        "chat": {"id": 777, "type": "private"},
                        "text": "/spawn brain summarize status",
                    },
                },
                snapshots,
            )
            self.assertIn("uncertain", tg.sent[-1]["text"])
            self.assertNotIn("Swipe-reply to retry", tg.sent[-1]["text"])

            herdr.snapshots["w23:p1"] = make_snap(pane_id="w23:p1", status="done", terminal_id="term_23")
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 2)
            self.assertIn("✅ <b>Done</b>", tg.sent[-1]["text"])
            clock.advance(90.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 2)

    def test_idle_90s_debounce_cancels_when_resuming_work_and_fires_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr({"w15:p1": make_snap(pane_id="w15:p1", status="working", state_change_seq=1)})
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
            )
            daemon.poll_herdr_once()

            # Finishes turn at t=0 -> must NOT send at t=0 or t=50s
            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="done", state_change_seq=2)
            daemon.poll_herdr_once()
            clock.advance(50.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 0)

            # User prompts again at t=55s -> timer cancelled!
            clock.advance(5.0)
            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="working", state_change_seq=3)
            daemon.poll_herdr_once()

            # Finishes second turn at t=60s; focusing tab (done -> idle, seq+1) at t=80s preserves timer
            clock.advance(5.0)
            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="done", state_change_seq=4)
            daemon.poll_herdr_once()
            clock.advance(20.0)
            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="idle", state_change_seq=5)
            daemon.poll_herdr_once()
            clock.advance(69.0)  # 89s since t=60s
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 0)

            # Reaches 90s since t=60s -> fires exactly once
            clock.advance(1.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 1)

            # Subsequent polls while still idle do not re-fire
            clock.advance(300.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 1)

            # Unseen sub-poll turn (idle seq=5 -> idle seq=7) resets timer and alerts after another 90s
            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="idle", state_change_seq=7)
            daemon.poll_herdr_once()
            clock.advance(89.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 1)
            clock.advance(1.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 2)

    def test_blocked_30s_debounce_cancels_on_quick_clear_and_fires_at_30s(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr({"w15:p1": make_snap(pane_id="w15:p1", status="working", state_change_seq=1)})
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
            )
            daemon.poll_herdr_once()

            # Auto-answered or locally approved within 10s (< 30s) -> never sends
            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="blocked", state_change_seq=2)
            daemon.poll_herdr_once()
            clock.advance(10.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 0)
            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="working", state_change_seq=3)
            daemon.poll_herdr_once()
            clock.advance(60.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 0)

            # Real block held >= 30s fires once at 30s, and back-to-back seq=5 fires after another 30s
            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="blocked", state_change_seq=4)
            daemon.poll_herdr_once()
            clock.advance(29.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 0)
            clock.advance(1.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 1)
            self.assertIn("⚠️ <b>Blocked</b>", tg.sent[-1]["text"])

            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", status="blocked", state_change_seq=5)
            daemon.poll_herdr_once()
            clock.advance(30.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 2)

    def test_cowork_lead_only_filtering_suppresses_support_done_but_allows_blocked(self) -> None:
        lead = make_snap(pane_id="w22:p1", workspace_id="w22", name=None, status="working", state_change_seq=1)
        support = make_snap(
            pane_id="w22:p2",
            workspace_id="w22",
            name="cx-support-w22-p1",
            agent="codex",
            status="working",
            state_change_seq=1,
        )
        explicit_lead = make_snap(
            pane_id="w30:p1",
            workspace_id="w30",
            name="cl-lead-api",
            status="working",
            state_change_seq=1,
        )
        sibling_worker = make_snap(
            pane_id="w30:p2",
            workspace_id="w30",
            name="helper-pane",
            status="working",
            state_change_seq=1,
        )
        legacy_support = make_snap(pane_id="w31:p2", workspace_id="w31", name="ag-w31-p1")
        snaps = {
            "w22:p1": lead,
            "w22:p2": support,
            "w30:p1": explicit_lead,
            "w30:p2": sibling_worker,
            "w31:p2": legacy_support,
        }
        self.assertFalse(is_cowork_support(lead, snaps))
        self.assertTrue(is_cowork_support(support, snaps))
        self.assertFalse(is_cowork_support(explicit_lead, snaps))
        self.assertTrue(is_cowork_support(sibling_worker, snaps))
        self.assertTrue(is_cowork_support(legacy_support, snaps))

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr(
                {
                    "w22:p1": lead,
                    "w22:p2": support,
                    "w30:p1": explicit_lead,
                    "w30:p2": sibling_worker,
                }
            )
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
            )
            daemon.poll_herdr_once()

            # All 4 finish working -> after 90s ONLY w22:p1 and w30:p1 notify!
            for pid, s in list(herdr.snapshots.items()):
                herdr.snapshots[pid] = make_snap(
                    pane_id=s.pane_id,
                    workspace_id=s.workspace_id,
                    agent=s.agent,
                    status="idle",
                    name=s.name,
                    state_change_seq=2,
                )
            daemon.poll_herdr_once()
            clock.advance(95.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 2)
            notified_texts = "\n".join(m["text"] for m in tg.sent)
            self.assertIn("w22:p1", notified_texts)
            self.assertIn("w30:p1", notified_texts)
            self.assertNotIn("cx-support-w22-p1", notified_texts)
            self.assertNotIn("w30:p2", notified_texts)

            # Support reviewer blocks on an approval prompt for 30s -> DOES notify!
            herdr.snapshots["w22:p2"] = make_snap(
                pane_id="w22:p2",
                workspace_id="w22",
                agent="codex",
                status="blocked",
                name="cx-support-w22-p1",
                state_change_seq=3,
            )
            daemon.poll_herdr_once()
            clock.advance(30.0)
            daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 3)
            self.assertIn("Migration (🤖 Codex)", tg.sent[-1]["text"])
            self.assertIn("⚠️ <b>Blocked</b>", tg.sent[-1]["text"])
            self.assertEqual(tg.sent[-1]["reply_markup"]["inline_keyboard"][-1][-1]["callback_data"], "read:w22:p2")

    def test_dynamic_blocked_buttons_numbered_4_options_and_prose_list_rejection(self) -> None:
        four_opt_dialog = (
            "────────────────────────────────────────────────────────────────────\n"
            " ☐ Deployment strategy\n\n"
            " Which rollout policy should we use?\n\n"
            " ❯ 1. Rolling deploy (Recommended)\n"
            "      Gradual zero-downtime rollout\n"
            "   2. Blue-green cutover\n"
            "      Swap active target group\n"
            "   3. Canary 5%\n"
            "      Verify metrics before full traffic\n"
            "   4. Type custom strategy\n\n"
            " Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
        )
        menu = extract_blocked_menu(four_opt_dialog)
        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.mode, "keys")
        self.assertEqual(len(menu.options), 4)
        self.assertEqual([o.key for o in menu.options], ["1", "2", "3", "4"])

        kb = build_transition_keyboard("blocked", "w15:p1", state_change_seq=42, dialog_text=four_opt_dialog)
        rows = kb["inline_keyboard"]
        self.assertEqual(len(rows), 5)  # 4 option rows + 1 utility row
        self.assertIn("1. Rolling deploy", rows[0][0]["text"])
        self.assertEqual(rows[0][0]["callback_data"], f"key:w15:p1:42~{menu.digest}:1")
        self.assertIn("4. Type custom strategy", rows[3][0]["text"])
        self.assertEqual(rows[3][0]["callback_data"], f"key:w15:p1:42~{menu.digest}:4")

        # Prose numbered list above horizontal rule or above trailing [y/n] question is NOT parsed as menu
        prose_above_yn = (
            "Summary of changes:\n"
            "1. Updated parser\n"
            "2. Added unit tests\n"
            "3. Cleaned docs\n"
            "────────────────────────────────────────────\n"
            "Allow command `git status`? [y/n]\n"
        )
        self.assertIsNone(extract_blocked_menu(prose_above_yn))

        prose_without_hr = (
            "Summary of changes:\n1. Updated parser\n2. Added unit tests\n\nAllow command `git status`? [y/n]\n"
        )
        self.assertIsNone(extract_blocked_menu(prose_without_hr))

    def test_dynamic_blocked_cursor_menu_opt_callback_live_delta_digest_and_timeout_safety(self) -> None:
        initial_visible = (
            " Requesting permission for: bash\n"
            "   pytest -q\n\n"
            " Do you want to proceed?\n"
            " ❯ Allow once\n"
            "   Allow for this session\n"
            "   Allow always in workspace\n"
            "   Deny\n\n"
            " ↑/↓ to navigate · Enter to select · Esc to cancel\n"
        )
        moved_visible = initial_visible.replace(
            " ❯ Allow once\n   Allow for this session",
            "   Allow once\n ❯ Allow for this session",
        )
        changed_visible = initial_visible.replace("   Deny", "   Delete everything")

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr(
                {"w15:p1": make_snap(pane_id="w15:p1", agent="agy", status="blocked", state_change_seq=10)}
            )
            herdr.visible_outputs["w15:p1"] = initial_visible
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
            )
            daemon.poll_herdr_once()
            clock.advance(30.0)
            snapshots = daemon.poll_herdr_once()
            self.assertEqual(len(tg.sent), 1)
            rows = tg.sent[-1]["reply_markup"]["inline_keyboard"]
            self.assertEqual(len(rows), 5)  # 4 cursor options + utility row
            deny_cb = rows[3][0]["callback_data"]
            self.assertTrue(deny_cb.startswith(f"opt:w15:p1:10@{term_tag('term_1')}:3:"))
            self.assertLessEqual(len(deny_cb.encode("utf-8")), 64)

            # 1. If dialog options changed before click, digest mismatch rejects without sending keys
            herdr.visible_outputs["w15:p1"] = changed_visible
            click = {
                "update_id": 1,
                "callback_query": {
                    "id": "cq_mismatch",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 777, "type": "private"}},
                    "data": deny_cb,
                },
            }
            daemon.handle_update(click, snapshots)
            self.assertEqual(herdr.keys, [])
            self.assertIn("Dialog options changed", tg.callbacks[-1][1])

            # 2. Local cursor moved from 0 -> 1 without seq change: live re-read computes delta = 3 - 1 = 2 downs + enter!
            herdr.visible_outputs["w15:p1"] = moved_visible
            daemon.handle_update(click, snapshots)
            self.assertEqual(herdr.keys, [("w15:p1", ["down", "down", "enter"])])

            # 3. Uncertain send-keys timeout keeps approval consumed so multi-key navigation cannot double-fire
            herdr.snapshots["w15:p1"] = make_snap(pane_id="w15:p1", agent="agy", status="blocked", state_change_seq=11)
            menu11 = extract_blocked_menu(moved_visible)
            assert menu11 is not None
            herdr.send_keys_status = "ambiguous"
            herdr.send_keys_err = "herdr send-keys timed out"
            timeout_click = {
                "update_id": 2,
                "callback_query": {
                    "id": "cq_to",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 777, "type": "private"}},
                    "data": f"opt:w15:p1:11:3:{menu11.digest}",
                },
            }
            daemon.handle_update(timeout_click, snapshots)
            self.assertEqual(len(herdr.keys), 2)
            # Re-clicking after timeout is rejected as stale/consumed
            herdr.send_keys_status = "sent"
            daemon.handle_update(timeout_click, snapshots)
            self.assertEqual(len(herdr.keys), 2)

    def test_extract_last_agent_turn_and_3_section_flash_digest_and_security_isolation(self) -> None:
        raw_tui = (
            "❯ old prompt\n"
            "⏺ Old response that should be sliced away.\n\n"
            "❯ audit the auth handler\n"
            "⏺ Read(src/auth.py)\n"
            "  ⎿  Read 128 lines\n"
            "⠋ Thinking…\n"
            "⏺ Audited `src/auth.py` — found a missing token check with sk-abcdefghijklmnopqrstuvwxyz123456.\n"
            "- Patched **verify_session** and ran `<script>alert(1)</script>` escaping tests.\n"
            "────────────────────────────────────────────────────────────\n"
            "❯ \n"
            "────────────────────────────────────────────────────────────\n"
            "  ? for shortcuts · 84% context left · bypass permissions\n"
        )
        cleaned = extract_last_agent_turn(raw_tui, agent_kind="claude")
        self.assertNotIn("Old response", cleaned)
        self.assertNotIn("Read 128 lines", cleaned)
        self.assertNotIn("? for shortcuts", cleaned)
        self.assertNotIn("sk-abcdef", cleaned)
        self.assertNotIn("—", cleaned)
        self.assertIn("Audited `src/auth.py`", cleaned)

        # Short output renders clean HTML with escaped <script> and no em dashes
        short_msg = format_done_message(make_snap(title="Auth — review"), cleaned, digest=None, include_output=True)
        self.assertNotIn("—", short_msg)
        self.assertNotIn("<script>", short_msg)
        self.assertIn("&lt;script&gt;", short_msg)
        self.assertIn("<code>src/auth.py</code>", short_msg)
        self.assertIn("<b>verify_session</b>", short_msg)

        # AgyFlashSummarizer subprocess isolation: --sandbox, all 6 deny rules, private temp cwd, scrubbed env
        captured_call: dict[str, Any] = {}

        def fake_agy_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured_call["cmd"] = cmd
            captured_call["kwargs"] = kwargs
            captured_call["cwd_exists_during_run"] = Path(kwargs["cwd"]).is_dir()
            inner = {
                "done": ["Patched `src/auth.py` — added token verification"],
                "findings": [
                    "[P1] Unauthenticated fallback allowed <img src=x> with sk-abcdefghijklmnopqrstuvwxyz123456"
                ],
                "attention": [],
            }
            outer = {"session_id": "s1", "response": "```json\n" + __import__("json").dumps(inner) + "\n```"}
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=__import__("json").dumps(outer), stderr=""
            )

        long_input = cleaned + "\n" + ("Detailed test verification line with context.\n" * 12)
        summarizer = AgyFlashSummarizer(model="gemini-3.8-flash-low", min_chars=350, binary="/usr/bin/agy")
        with patch.dict(
            "os.environ", {"TELEGRAM_BOT_TOKEN": "999999999:AAHsecretbot_token_value_1234567890", "HOME": "/tmp"}
        ):
            with patch("subprocess.run", side_effect=fake_agy_run):
                digest = summarizer.summarize(long_input)

        self.assertIsNotNone(digest)
        assert digest is not None
        cmd = captured_call["cmd"]
        self.assertEqual(cmd[0:4], ["/usr/bin/agy", "--model", "gemini-3.8-flash-low", "--sandbox"])
        self.assertIn(SUMMARY_DENY_SETTINGS_JSON, cmd)
        for denied_tool in ("command(*)", "read_file(*)", "write_file(*)", "read_url(*)", "execute_url(*)", "mcp(*)"):
            self.assertIn(denied_tool, SUMMARY_DENY_SETTINGS_JSON)
        self.assertEqual(cmd[-2], "-p")
        self.assertIn("<untrusted_terminal_output>", cmd[-1])
        self.assertTrue(captured_call["cwd_exists_during_run"])
        self.assertIn("herdr-tg-sum-", Path(captured_call["kwargs"]["cwd"]).name)
        self.assertFalse(Path(captured_call["kwargs"]["cwd"]).exists())
        self.assertEqual(captured_call["kwargs"]["errors"], "replace")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", captured_call["kwargs"]["env"])

        rendered_digest = format_done_message(make_snap(), long_input, digest=digest, include_output=True)
        self.assertNotIn("<b>What was done</b>", rendered_digest)
        self.assertNotIn("<b>Findings</b>", rendered_digest)
        self.assertIn("🔴 <b>[P1]</b>", rendered_digest)
        self.assertNotIn("Needs your attention", rendered_digest)  # Empty section omitted
        self.assertNotIn("Swipe-reply", rendered_digest)
        self.assertNotIn("<img", rendered_digest)
        self.assertNotIn("sk-abcdef", rendered_digest)
        self.assertNotIn("—", rendered_digest)
        self.assertLessEqual(len(rendered_digest), 4000)

        # include_output=False skips both terminal reads and LLM summarization
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr({"w15:p1": make_snap(status="working", state_change_seq=1)})
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                include_output=False,
                clock=clock,
            )
            daemon.poll_herdr_once()
            herdr.snapshots["w15:p1"] = make_snap(status="done", state_change_seq=2)
            daemon.poll_herdr_once()
            clock.advance(90.0)
            daemon.poll_herdr_once()
            self.assertEqual(herdr.read_calls, [])
            self.assertEqual(herdr.footer_peeks, [])
            self.assertEqual(len(tg.sent), 1)

    def test_async_summary_discards_stale_generation_and_caches_across_transient_telegram_error(self) -> None:
        class CountingSummarizer(AgyFlashSummarizer):
            def __init__(self) -> None:
                super().__init__(model="gemini-3.8-flash-low", min_chars=50)
                self.calls = 0

            def summarize(self, cleaned_turn: str, **_: Any) -> DigestSections | None:
                self.calls += 1
                return DigestSections(
                    done=(f"Summary call #{self.calls}: {cleaned_turn[:30]}",),
                    findings=(),
                    attention=("Approve prod rollout? Recommend yes after staging check.",),
                )

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr({"w15:p1": make_snap(status="working", state_change_seq=1)})
            herdr.outputs["w15:p1"] = "Completed round 1 migration checks. " * 6
            # Fail the first actual Telegram send (which will happen on round 2's delivery)
            tg = FakeTelegram(fail_on_call=1)
            summarizer = CountingSummarizer()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                summarizer=summarizer,
                async_summaries=True,
                clock=clock,
            )
            try:
                daemon.poll_herdr_once()

                # Round 1 hits 90s and schedules async summary
                herdr.snapshots["w15:p1"] = make_snap(status="idle", state_change_seq=2)
                daemon.poll_herdr_once()
                clock.advance(90.0)
                daemon.poll_herdr_once()
                daemon.drain_summary_jobs()
                self.assertEqual(summarizer.calls, 1)
                self.assertEqual(len(tg.sent), 0)

                # Before next poll delivers Round 1, agent resumes working -> idle (generation increments!)
                herdr.snapshots["w15:p1"] = make_snap(status="working", state_change_seq=3)
                daemon.poll_herdr_once()  # Drains & discards stale Round 1 result!
                self.assertEqual(len(tg.sent), 0)

                herdr.outputs["w15:p1"] = "Completed round 2 final verification. " * 6
                herdr.snapshots["w15:p1"] = make_snap(status="idle", state_change_seq=4)
                daemon.poll_herdr_once()
                clock.advance(90.0)
                daemon.poll_herdr_once()  # Submits Round 2 summary job
                daemon.drain_summary_jobs()
                self.assertEqual(summarizer.calls, 2)

                # Next poll drains Round 2 summary, tries Telegram send #1 -> transient failure!
                clock.advance(2.0)
                daemon.poll_herdr_once()
                self.assertEqual(len(tg.sent), 0)

                # Retry poll sends cached prepared_text WITHOUT invoking summarizer a 3rd time!
                clock.advance(2.0)
                daemon.poll_herdr_once()
                self.assertEqual(summarizer.calls, 2)
                self.assertEqual(len(tg.sent), 1)
                self.assertIn("Summary call #2", tg.sent[-1]["text"])
                self.assertIn("<b>Needs your attention</b>", tg.sent[-1]["text"])
            finally:
                daemon.close()

    def test_cursor_menu_column_alignment_wrapped_descriptions_and_context_digest_toctou(self) -> None:
        # 1. Wrapped option descriptions indented deeper than label_col are skipped, NOT counted as extra options
        wrapped_desc_dialog = (
            " Requesting permission for: bash\n"
            "   pytest -q\n\n"
            " Do you want to proceed?\n"
            "   Allow once\n"
            "     Run only this single pytest command\n"
            " ❯ Allow for this session\n"
            "     Remember approval until terminal exit\n"
            "   Deny\n\n"
            " ↑/↓ to navigate · Enter to select · Esc to cancel\n"
        )
        menu = extract_blocked_menu(wrapped_desc_dialog)
        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.mode, "cursor")
        self.assertEqual([o.label for o in menu.options], ["Allow once", "Allow for this session", "Deny"])
        self.assertEqual(menu.active_index, 1)

        # Mid-menu unaligned prose rejects cursor parsing cleanly
        corrupted_dialog = (
            " Do you want to proceed?\n ❯ Allow once\n Unaligned warning note in middle\n   Deny\n\n Enter to select\n"
        )
        self.assertIsNone(extract_blocked_menu(corrupted_dialog))

        # 2. Command context changing under identical option labels invalidates digest!
        dangerous_same_labels = wrapped_desc_dialog.replace("pytest -q", "rm -rf /important")
        dangerous_menu = extract_blocked_menu(dangerous_same_labels)
        self.assertIsNotNone(dangerous_menu)
        assert dangerous_menu is not None
        self.assertNotEqual(menu.digest, dangerous_menu.digest)

        # 3. TOCTOU: if prompt seq or terminal_id changes DURING read_pane_visible, callback aborts before send_keys
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr(
                {"w15:p1": make_snap(pane_id="w15:p1", agent="agy", status="blocked", state_change_seq=10)}
            )
            herdr.visible_outputs["w15:p1"] = wrapped_desc_dialog
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
            )
            daemon.poll_herdr_once()
            clock.advance(30.0)
            snapshots = daemon.poll_herdr_once()
            opt0_cb = tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

            orig_read_vis = herdr.read_pane_visible

            def mutating_read_vis(target: str, lines: int = 40) -> str:
                out = orig_read_vis(target, lines=lines)
                # Simulate prompt advancing from seq=10 to seq=11 mid-read!
                herdr.snapshots["w15:p1"] = make_snap(
                    pane_id="w15:p1", agent="agy", status="blocked", state_change_seq=11
                )
                return out

            herdr.read_pane_visible = mutating_read_vis  # type: ignore[method-assign]
            daemon.handle_update(
                {
                    "update_id": 5,
                    "callback_query": {
                        "id": "cq_race",
                        "from": {"id": 777},
                        "message": {"chat": {"id": 777, "type": "private"}},
                        "data": opt0_cb,
                    },
                },
                snapshots,
            )
            self.assertEqual(herdr.keys, [])
            self.assertIn("no longer waiting", tg.callbacks[-1][1])

            # 4. Terminal replacement with identical pane_id + seq=10 is rejected by @term4 tag
            herdr.read_pane_visible = orig_read_vis  # type: ignore[method-assign]
            herdr.snapshots["w15:p1"] = make_snap(
                pane_id="w15:p1", agent="agy", status="blocked", terminal_id="term_2", state_change_seq=10
            )
            daemon.handle_update(
                {
                    "update_id": 6,
                    "callback_query": {
                        "id": "cq_term",
                        "from": {"id": 777},
                        "message": {"chat": {"id": 777, "type": "private"}},
                        "data": opt0_cb,
                    },
                },
                snapshots,
            )
            self.assertEqual(herdr.keys, [])
            self.assertIn("terminal changed", tg.callbacks[-1][1])

    def test_send_keys_transport_ambiguity_short_turns_subpoll_done_and_digest_shrinking(self) -> None:
        # 1. HerdrClient.send_keys_detailed treats unclassified errors as ambiguous, known codes as failed
        client = HerdrClient(binary="/usr/bin/herdr")
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["/usr/bin/herdr"],
                returncode=1,
                stdout="",
                stderr="broken pipe after write",
            ),
        ):
            status_ambig, _ = client.send_keys_detailed("w15:p1", ["y"])
        self.assertEqual(status_ambig, "ambiguous")

        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["/usr/bin/herdr"],
                returncode=1,
                stdout="",
                stderr='{"error":{"code":"pane_not_found","message":"no such pane"}}',
            ),
        ):
            status_fail, _ = client.send_keys_detailed("w15:p1", ["y"])
        self.assertEqual(status_fail, "failed")

        # 2. Short final reply ("Done.") and boxed prompt slice away prior turns cleanly
        short_turn_raw = (
            "│ ❯ refactor big module │\nOld long output line 1\nOld long output line 2\n│ ❯ ship it │\nDone.\n"
        )
        self.assertEqual(extract_last_agent_turn(short_turn_raw), "Done.")

        # 3. Broader cowork role prefixes (qa-lead-api / sec-support-auth)
        qa_lead = make_snap(pane_id="w40:p1", workspace_id="w40", name="qa-lead-api")
        qa_worker = make_snap(pane_id="w40:p2", workspace_id="w40", name="worker-pane")
        sec_support = make_snap(pane_id="w41:p2", workspace_id="w41", name="sec-support-auth")
        self.assertFalse(is_cowork_support(qa_lead, {"w40:p1": qa_lead, "w40:p2": qa_worker}))
        self.assertTrue(is_cowork_support(qa_worker, {"w40:p1": qa_lead, "w40:p2": qa_worker}))
        self.assertTrue(is_cowork_support(sec_support, {"w41:p2": sec_support}))

        # 4. Tight HTML budget with entity-heavy text terminates cleanly with balanced tags
        entity_digest = DigestSections(
            done=("<tag>&" * 80,),
            findings=("[P1] " + ("<err>&" * 60),),
            attention=("Choose <A> & <B>?" * 20,),
        )
        shrunk = _render_digest_sections_html(entity_digest, max_chars=90)
        self.assertLessEqual(len(shrunk), 90)
        self.assertEqual(shrunk.count("<b>"), shrunk.count("</b>"))
        self.assertNotRegex(shrunk, r"&(?!(?:lt|gt|amp|quot|#x27);)")

        # 5. Sub-poll blocked -> idle (seq+2 vs Esc seq+1) + worker exception fallback + pane recreation generation safety
        class ExplodingSummarizer(AgyFlashSummarizer):
            def __init__(self) -> None:
                super().__init__(model="gemini-3.8-flash-low", min_chars=20)

            def summarize(self, cleaned_turn: str, **_: Any) -> DigestSections | None:
                raise RuntimeError("boom inside worker")

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr({"w15:p1": make_snap(status="blocked", state_change_seq=10)})
            herdr.outputs["w15:p1"] = "Completed fast work after approval with plenty of text for summary."
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                summarizer=ExplodingSummarizer(),
                async_summaries=True,
                clock=clock,
            )
            try:
                daemon.poll_herdr_once()

                # Esc cancel (blocked seq=10 -> idle seq=11, delta=1) does NOT trigger Done alert
                herdr.snapshots["w15:p1"] = make_snap(status="idle", state_change_seq=11)
                daemon.poll_herdr_once()
                clock.advance(100.0)
                daemon.poll_herdr_once()
                self.assertEqual(len(tg.sent), 0)

                # Block again (seq=12), then sub-poll approve+work+finish -> idle (seq=14, delta=2) triggers Done!
                herdr.snapshots["w15:p1"] = make_snap(status="blocked", state_change_seq=12)
                daemon.poll_herdr_once()
                herdr.snapshots["w15:p1"] = make_snap(status="idle", state_change_seq=14)
                daemon.poll_herdr_once()
                clock.advance(90.0)
                daemon.poll_herdr_once()  # Submits exploding worker job
                daemon.drain_summary_jobs()
                clock.advance(2.0)
                daemon.poll_herdr_once()  # Recovers with deterministic HTML fallback!
                self.assertEqual(len(tg.sent), 1)
                self.assertIn("✅ <b>Done</b>", tg.sent[-1]["text"])
                self.assertIn("Completed fast work after approval", tg.sent[-1]["text"])
            finally:
                daemon.close()

    def test_prompt_wait_stalled_ambiguous_and_live_blocked_refresh(self) -> None:
        client = HerdrClient(binary="/usr/bin/herdr")
        stalled_json = (
            '{"id":"cli:agent:prompt","error":{"code":"agent_prompt_stalled",'
            '"message":"agent prompt produced no observed state change within 5000 ms; '
            'status is idle and state_change_seq remained 3571"}}'
        )
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["/usr/bin/herdr"],
                returncode=1,
                stdout="",
                stderr=stalled_json,
            ),
        ):
            status, err = client.prompt_agent_detailed("w1Y:p1", "deploy rest")
            ok, ok_err = client.prompt_agent("w1Y:p1", "deploy rest")
        self.assertEqual(status, "rejected")
        self.assertFalse(ok)
        self.assertIn("agent_prompt_stalled", err)
        self.assertEqual(err, ok_err)

        with tempfile.TemporaryDirectory() as tmp:
            herdr = FakeHerdr({"w1Y:p1": make_snap(pane_id="w1Y:p1", workspace_label="acme", state_change_seq=10)})
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
            )
            try:
                snapshots = daemon.poll_herdr_once()

                # 1. Stalled prompt reports failure with Read output button instead of false Sent
                herdr.prompt_ok = False
                herdr.prompt_err = "agent_prompt_stalled: agent prompt produced no observed state change within 5000 ms"
                daemon.handle_update(
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 91,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "w1Y:p1: deploy rest",
                        },
                    },
                    snapshots,
                )
                self.assertEqual(len(tg.sent), 1)
                self.assertIn("❌ Could not prompt", tg.sent[-1]["text"])
                self.assertIn("agent_prompt_stalled", tg.sent[-1]["text"])
                markup = tg.sent[-1]["reply_markup"]
                self.assertIsNotNone(markup)
                self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "read:w1Y:p1")

                # 2. Ambiguous delivery warns user before resending
                herdr.prompt_ok = False
                herdr.prompt_detail_status = "ambiguous"
                herdr.prompt_err = "herdr agent prompt timed out"
                daemon.handle_update(
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 92,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "w1Y:p1: check status",
                        },
                    },
                    snapshots,
                )
                self.assertEqual(len(tg.sent), 2)
                self.assertIn("⚠️ Prompt delivery to", tg.sent[-1]["text"])
                self.assertIn("is uncertain", tg.sent[-1]["text"])
                self.assertEqual(
                    tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                    "read:w1Y:p1",
                )

                # 3. Rejected because agent transitioned to blocked mid-poll refreshes live state_change_seq
                herdr.prompt_detail_status = "rejected"
                herdr.prompt_err = "agent_blocked: agent is waiting on a dialog"
                herdr.snapshots["w1Y:p1"] = make_snap(
                    pane_id="w1Y:p1",
                    workspace_label="acme",
                    status="blocked",
                    state_change_seq=77,
                )
                herdr.visible_outputs["w1Y:p1"] = "Allow command?\n1. Yes\n2. No\nEnter to select · Esc to cancel"
                daemon.handle_update(
                    {
                        "update_id": 3,
                        "message": {
                            "message_id": 93,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "w1Y:p1: continue",
                        },
                    },
                    snapshots,
                )
                self.assertEqual(len(tg.sent), 3)
                self.assertIn("❌ Could not prompt", tg.sent[-1]["text"])
                blocked_markup = tg.sent[-1]["reply_markup"]
                flat_callbacks = [btn["callback_data"] for row in blocked_markup["inline_keyboard"] for btn in row]
                self.assertTrue(any(":77@" in cb for cb in flat_callbacks))
            finally:
                daemon.close()

    def test_model_detection_numbering_formatting_and_routing(self) -> None:
        self.assertEqual(
            detect_pane_model("  🤖 Opus 5.5 (high)  |  🧠 85%  |  🟢 5h 11%\n·\n", "claude"),
            "opus",
        )
        self.assertEqual(
            detect_pane_model("Ctx 18.4% │ Gemini 2.5 Flash medium │ main\n", "agy"),
            "flash",
        )
        self.assertEqual(
            detect_pane_model("? for shortcuts                    auto · Gemini 2.5 Pro · high\n", "agy"),
            "pro",
        )
        self.assertEqual(
            detect_pane_model("  gpt-5-codex max · Context 37% used…\n", "codex"),
            "codex",
        )

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            herdr = FakeHerdr(
                {
                    "w21:p1": make_snap(
                        pane_id="w21:p1",
                        workspace_id="w21",
                        workspace_label="Auth",
                        agent="agy",
                        status="idle",
                        terminal_id="t21",
                        state_change_seq=5,
                    ),
                    "w22:p1": make_snap(
                        pane_id="w22:p1",
                        workspace_id="w22",
                        workspace_label="Upgrade",
                        agent="agy",
                        status="blocked",
                        terminal_id="t22",
                        state_change_seq=8,
                    ),
                    "w15:p1": make_snap(
                        pane_id="w15:p1",
                        workspace_id="w15",
                        workspace_label="Migration",
                        agent="claude",
                        status="working",
                        terminal_id="t15",
                        state_change_seq=3,
                    ),
                }
            )
            herdr.footer_outputs = {
                "w21:p1": "Ctx 21.0% │ Gemini 2.5 Flash medium │ main\n",
                "w22:p1": "? for shortcuts          plan · Gemini 2.5 Flash · high\n",
                "w15:p1": "  🤖 Opus 5.5 (high)  |  🧠 72%\n·\n",
            }
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=state_dir / "state.json",
            )
            try:
                snapshots = daemon.poll_herdr_once()
                self.assertEqual(snapshots["w21:p1"].friendly_handle, "flash1")
                self.assertEqual(snapshots["w22:p1"].friendly_handle, "flash2")
                self.assertEqual(snapshots["w15:p1"].friendly_handle, "opus")

                status_html = format_status_overview(snapshots, pinned_target="w21:p1")
                self.assertIn("🤖 <code>Flash1</code>", status_html)
                self.assertIn("🤖 <code>Flash2</code>", status_html)
                self.assertIn("🤖 <code>Opus</code>", status_html)
                self.assertNotIn("w21:p1", status_html)
                self.assertNotIn("w22:p1", status_html)
                self.assertNotIn("w15:p1", status_html)

                # Prefix routing with flash1:, Flash2:, and opus: (case-insensitive)
                for idx, (msg_txt, expected_pane, expected_body) in enumerate(
                    [
                        ("flash1: run patrol", "w21:p1", "run patrol"),
                        ("Flash2: check logs", "w22:p1", "check logs"),
                        ("opus: review migration", "w15:p1", "review migration"),
                    ],
                    start=1,
                ):
                    daemon.handle_update(
                        {
                            "update_id": idx,
                            "message": {
                                "message_id": 10 + idx,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "text": msg_txt,
                            },
                        },
                        snapshots,
                    )
                    self.assertEqual(herdr.prompts[-1], (expected_pane, expected_body))
                self.assertIn("Sent to <b>Migration (🤖 Opus)</b>", tg.sent[-1]["text"])
                self.assertNotIn("w15:p1", tg.sent[-1]["text"])

                # Slash commands (/target, /read, /key) accept model handles and hide raw pane IDs
                daemon.handle_update(
                    {
                        "update_id": 10,
                        "message": {
                            "message_id": 20,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "/target flash2",
                        },
                    },
                    snapshots,
                )
                self.assertEqual(daemon.state.pinned_target, "w22:p1")
                self.assertIn("Upgrade (🤖 Flash2)", tg.sent[-1]["text"])
                self.assertNotIn("w22:p1", tg.sent[-1]["text"])

                daemon.handle_update(
                    {
                        "update_id": 11,
                        "message": {
                            "message_id": 21,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "/key flash2 y",
                        },
                    },
                    snapshots,
                )
                self.assertEqual(herdr.keys[-1], ("w22:p1", ["y"]))
                self.assertIn("Upgrade (🤖 Flash2)", tg.sent[-1]["text"])
                self.assertNotIn("w22:p1", tg.sent[-1]["text"])

                # Swipe-reply to proactive telegram-send or Done card works via header regex (with or without 🤖)
                t_hdr, b_hdr = resolve_reply_target(
                    "looks good",
                    {"message_id": 501, "text": "💬 Auth (🤖 Flash1)\n🟢 HTTP checks passed"},
                    daemon.state,
                    snapshots,
                )
                self.assertEqual((t_hdr, b_hdr), ("w21:p1", "looks good"))

                t_done, b_done = resolve_reply_target(
                    "ship it",
                    {"message_id": 502, "text": "✅ Done · Migration (🤖 Opus)\n\n• Patched auth check."},
                    daemon.state,
                    snapshots,
                )
                self.assertEqual((t_done, b_done), ("w15:p1", "ship it"))

                # Even if flash1 closes (so w22:p1 shifts from flash2 -> flash), recorded message_id binds to w22:p1!
                record_proactive_target(77701, "w22:p1", state_dir=state_dir)
                del herdr.snapshots["w21:p1"]
                shifted = daemon.poll_herdr_once()
                self.assertEqual(shifted["w22:p1"].friendly_handle, "flash")
                daemon.handle_update(
                    {
                        "update_id": 12,
                        "message": {
                            "message_id": 22,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "deploy it",
                            "reply_to_message": {
                                "message_id": 77701,
                                "text": "💬 Upgrade (🤖 Flash2)\nReady to deploy.",
                            },
                        },
                    },
                    shifted,
                )
                self.assertEqual(herdr.prompts[-1], ("w22:p1", "deploy it"))
            finally:
                daemon.close()

    def test_turn_summarizer_defaults_checks_passed_and_severity_badges(self) -> None:
        self.assertEqual(DEFAULT_SUMMARY_PROVIDER, "auto")
        self.assertEqual(DEFAULT_SUMMARY_MODEL, "")
        default_sum = AgyFlashSummarizer()
        self.assertEqual(default_sum.provider, "agy")
        self.assertEqual(default_sum.model, "gemini-2.5-flash")
        self.assertEqual(default_sum.timeout_seconds, 40.0)
        prompt_sample = TurnSummarizer._build_prompt("sample")
        self.assertIn("I opened PR #142", prompt_sample)
        self.assertIn("automated review rounds", prompt_sample)
        self.assertIn("currently undergoing checks before merging", prompt_sample)

        # Head + tail bounding preserves both opening BLUF and closing summary
        huge_turn = "BLUF opening summary sentence.\n" + ("middle filler line\n" * 600) + "Closing question for user?"
        bounded = TurnSummarizer._bound_input(huge_turn)
        self.assertLessEqual(len(bounded), 8000)
        self.assertIn("BLUF opening summary sentence.", bounded)
        self.assertIn("Closing question for user?", bounded)

        raw_json = (
            '{"done": ["**Health sweep:** Ran daily production health check across all service stacks."],'
            ' "checks": ["HTTP endpoints: 12/12 healthy", "systemd services: all active", "TLS certs: >30d remaining"],'
            ' "findings": ["[P1] **billing-api:** disk at 94% on `/var/log`",'
            ' "[P2] **metrics-worker:** memory at 81% — watch over next 24h",'
            ' "[Note] **acme-auth:** scheduled maintenance window tomorrow"],'
            ' "attention": ["Prune `/var/log/journal` on `billing-api`? Recommend yes to reclaim 14 GB."]}'
        )
        parsed = parse_digest_json(raw_json)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(len(parsed.checks), 3)

        snap = assign_model_handles(
            {"w21:p1": make_snap(pane_id="w21:p1", workspace_label="ProdSupport", agent="agy")}, {"w21:p1": "flash"}
        )["w21:p1"]
        msg = format_done_message(snap, "long turn", digest=parsed, include_output=True)
        self.assertIn("ProdSupport (🤖 Flash)", msg)
        self.assertNotIn("w21:p1", msg)
        self.assertNotIn("<b>What was done</b>", msg)
        self.assertNotIn("<b>Checks passed</b>", msg)
        self.assertNotIn("<b>Findings</b>", msg)
        self.assertIn("<b>Needs your attention</b>", msg)
        self.assertEqual(msg.count("🟢 "), 3)
        self.assertIn("🔴 <b>[P1]</b>", msg)
        self.assertIn("🟡 <b>[P2]</b>", msg)
        self.assertIn("📝 <b>[Note]</b>", msg)
        self.assertNotIn("—", msg)
        self.assertNotIn("Swipe-reply", msg)

    def test_codex_placeholder_and_agy_trace_extraction_fixes(self) -> None:
        codex_raw = (
            "› review the ts-preview diff\n\n"
            "• Reviewed `ts-preview` and verified all 13 unit tests pass.\n"
            "  - IPv6 loopback detection handles `[::1]` listeners cleanly.\n\n"
            "› Ask Codex to do anything\n\n"
            "  gpt-5-codex max · Context 37% used…\n"
        )
        codex_turn = extract_last_agent_turn(codex_raw, agent_kind="codex")
        self.assertIn("Reviewed `ts-preview`", codex_turn)
        self.assertIn("IPv6 loopback detection", codex_turn)
        self.assertNotIn("Ask Codex to do anything", codex_turn)
        self.assertNotIn("gpt-5-codex", codex_turn)

        agy_raw = (
            "❯ run health sweep\n"
            "○ Thought for 24.6s (ctrl+o to view)\n"
            "● Ran 4 commands (ctrl+o to expand)\n"
            "All 12 production hosts passed HTTP and SSH health checks.\n"
            "- Verified `acme-api` and `billing-worker` systemd units.\n"
            "────────────────────────────────────────────────────────────\n"
            "❯ \n"
            "────────────────────────────────────────────────────────────\n"
            "Ctx 18.4% │ Gemini 2.5 Flash medium │ main\n"
        )
        agy_turn = extract_last_agent_turn(agy_raw, agent_kind="agy")
        self.assertIn("All 12 production hosts passed", agy_turn)
        self.assertNotIn("Thought for 24.6s", agy_turn)
        self.assertNotIn("Ran 4 commands", agy_turn)
        self.assertNotIn("Gemini 2.5 Flash medium", agy_turn)

    def test_prod_workspace_matching_and_false_positive_isolation(self) -> None:
        for prod_label in (
            "Prod Support",
            "prod-support",
            "ProdSupport",
            "production-support",
            "On Duty",
            "onduty",
            "Oncall",
            "incident-response",
        ):
            self.assertTrue(is_prod_workspace(prod_label), prod_label)

        for dev_label in ("Migration", "Admin", "Frontend", "Backend", "Worker", "Auth", "Upgrade"):
            self.assertFalse(is_prod_workspace(dev_label), dev_label)

        # Coding workspaces whose terminal title mentions "production support" must NOT match
        frontend_snap = make_snap(
            pane_id="w1C:p1",
            workspace_id="w1C",
            workspace_label="Frontend",
            title="Frontend client deployment via prod support",
        )
        self.assertFalse(is_prod_snapshot(frontend_snap))

        # Custom glob + exact workspace patterns
        custom = parse_prod_workspaces("ops*, incident-response")
        self.assertEqual(custom, ("ops*", "incident-response"))
        self.assertTrue(is_prod_workspace("Ops Patrol", custom))
        self.assertTrue(is_prod_workspace("Incident Response", custom))
        self.assertFalse(is_prod_workspace("Prod Support", custom))

        raw_snaps = {
            "w21:p1": make_snap(pane_id="w21:p1", workspace_label="Prod Support", agent="agy"),
            "w22:p1": make_snap(pane_id="w22:p1", workspace_label="Upgrade", agent="agy"),
        }
        models = {"w21:p1": "pro", "w22:p1": "pro"}
        single_bot_handles = assign_channel_model_handles(raw_snaps, models, split_prod=False)
        self.assertEqual(single_bot_handles["w21:p1"].friendly_handle, "pro1")
        self.assertEqual(single_bot_handles["w22:p1"].friendly_handle, "pro2")
        dual_bot_handles = assign_channel_model_handles(raw_snaps, models, split_prod=True)
        self.assertEqual(dual_bot_handles["w21:p1"].friendly_handle, "pro")
        self.assertEqual(dual_bot_handles["w22:p1"].friendly_handle, "pro")

    def test_dual_bot_routing_state_isolation_channel_handles_and_auto_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            clock = FakeClock()
            herdr = FakeHerdr(
                {
                    "w21:p1": make_snap(
                        pane_id="w21:p1",
                        workspace_id="w21",
                        workspace_label="Prod Support",
                        agent="agy",
                        status="working",
                        state_change_seq=1,
                    ),
                    "w22:p1": make_snap(
                        pane_id="w22:p1",
                        workspace_id="w22",
                        workspace_label="Upgrade",
                        agent="agy",
                        status="working",
                        state_change_seq=1,
                    ),
                    "w15:p1": make_snap(
                        pane_id="w15:p1",
                        workspace_id="w15",
                        workspace_label="Migration",
                        agent="claude",
                        status="idle",
                        state_change_seq=1,
                    ),
                }
            )
            herdr.footer_outputs["w21:p1"] = "Ctx 15% │ Gemini 2.5 Pro medium │ main"
            herdr.footer_outputs["w22:p1"] = "Ctx 22% │ Gemini 2.5 Pro medium │ main"
            herdr.footer_outputs["w15:p1"] = "  claude-opus-4-6 · 80% left"

            dev_tg = FakeTelegram()
            prod_tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=dev_tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=state_path,
                clock=clock,
                summarizer=AgyFlashSummarizer(model="off"),
                prod_telegram=prod_tg,  # type: ignore[arg-type]
                prod_chat_id="777",
            )
            try:
                snapshots = daemon.poll_herdr_once()
                # Channel-partitioned handles: each bot has a single pro pane, so both get bare 'pro'
                self.assertEqual(snapshots["w21:p1"].friendly_handle, "pro")
                self.assertEqual(snapshots["w22:p1"].friendly_handle, "pro")
                self.assertEqual(snapshots["w21:p1"].header_label, "Prod Support (🤖 Pro)")
                self.assertEqual(snapshots["w22:p1"].header_label, "Upgrade (🤖 Pro)")

                # Both pro panes finish -> Prod Support goes ONLY to prod_tg, Upgrade goes ONLY to dev_tg
                herdr.snapshots["w21:p1"] = make_snap(
                    pane_id="w21:p1",
                    workspace_id="w21",
                    workspace_label="Prod Support",
                    agent="agy",
                    status="done",
                    state_change_seq=2,
                )
                herdr.snapshots["w22:p1"] = make_snap(
                    pane_id="w22:p1",
                    workspace_id="w22",
                    workspace_label="Upgrade",
                    agent="agy",
                    status="done",
                    state_change_seq=2,
                )
                daemon.poll_herdr_once()
                # Prod Support skips the 90s wait and sends immediately; Upgrade waits 90s!
                self.assertEqual(len(prod_tg.sent), 1)
                self.assertEqual(len(dev_tg.sent), 0)
                clock.advance(95.0)
                snapshots = daemon.poll_herdr_once()

                self.assertEqual(len(prod_tg.sent), 1)
                self.assertEqual(len(dev_tg.sent), 1)
                self.assertIn("Prod Support (🤖 Pro)", prod_tg.sent[0]["text"])
                self.assertIn("Upgrade (🤖 Pro)", dev_tg.sent[0]["text"])
                # Both FakeTelegram instances assign message_id=101 -> proves per-bot message_targets isolation!
                self.assertEqual(prod_tg.sent[0]["message_id"], 101)
                self.assertEqual(dev_tg.sent[0]["message_id"], 101)

                # Blocked alert on Prod Support also routes ONLY to prod_tg
                herdr.snapshots["w21:p1"] = make_snap(
                    pane_id="w21:p1",
                    workspace_id="w21",
                    workspace_label="Prod Support",
                    agent="agy",
                    status="blocked",
                    state_change_seq=3,
                )
                daemon.poll_herdr_once()
                clock.advance(30.0)
                snapshots = daemon.poll_herdr_once()
                self.assertEqual(len(prod_tg.sent), 2)
                self.assertEqual(len(dev_tg.sent), 1)
                self.assertIn("⚠️ <b>Blocked</b>", prod_tg.sent[-1]["text"])

                # Swipe-reply to message_id=101 on prod_tg vs dev_tg never collides; update_id sequences are isolated too
                herdr.snapshots["w21:p1"] = make_snap(
                    pane_id="w21:p1",
                    workspace_id="w21",
                    workspace_label="Prod Support",
                    agent="agy",
                    status="idle",
                    state_change_seq=4,
                )
                snapshots = daemon.poll_herdr_once()

                now_ts = time.time()
                daemon.process_updates(
                    [
                        {
                            "update_id": 50,
                            "message": {
                                "message_id": 200,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "text": "check billing-api",
                                "reply_to_message": {"message_id": 101, "text": "alert"},
                            },
                        }
                    ],
                    snapshots,
                    bot_role="prod",
                )
                # message_id=101 on prod_tg is older than message_id=102 (Blocked), so earlier-message context is attached;
                # message_id=101 on dev_tg IS the latest message on dev_tg, so no wrapper is added there.
                self.assertEqual(
                    herdr.prompts[-1],
                    ("w21:p1", "[Replying to earlier Telegram message]\n> alert\n\ncheck billing-api"),
                )
                self.assertEqual(daemon.state.prod_last_update_id, 50)
                self.assertEqual(daemon.state.last_update_id, 0)

                daemon.process_updates(
                    [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 201,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "text": "ship the upgrade",
                                "reply_to_message": {"message_id": 101, "text": "alert"},
                            },
                        }
                    ],
                    snapshots,
                    bot_role="default",
                )
                self.assertEqual(herdr.prompts[-1], ("w22:p1", "ship the upgrade"))
                self.assertEqual(daemon.state.last_update_id, 1)

                # Unprefixed message in prod chat auto-routes to the single Prod Support pane!
                daemon.handle_update(
                    {
                        "update_id": 51,
                        "message": {
                            "message_id": 202,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "recheck all boxes",
                        },
                    },
                    snapshots,
                    bot_role="prod",
                )
                self.assertEqual(herdr.prompts[-1], ("w21:p1", "recheck all boxes"))

                # Unprefixed message in dev chat (2 dev panes, no pinned target) asks which agent should receive it
                prev_prompts = len(herdr.prompts)
                daemon.handle_update(
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 203,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "ambiguous prompt",
                        },
                    },
                    snapshots,
                    bot_role="default",
                )
                self.assertEqual(len(herdr.prompts), prev_prompts)
                self.assertIn("Which agent should receive this?", dev_tg.sent[-1]["text"])

                # /status scopes per bot channel with generic default headings unless renamed by user,
                # while /status all shows all agents
                daemon.handle_update(
                    {
                        "update_id": 52,
                        "message": {
                            "message_id": 204,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "/status",
                        },
                    },
                    snapshots,
                    bot_role="prod",
                )
                self.assertIn("<b>Herdr Bridge (prod)</b>", prod_tg.sent[-1]["text"])
                self.assertNotIn("Prod Support Agents", prod_tg.sent[-1]["text"])
                self.assertIn("Prod Support", prod_tg.sent[-1]["text"])
                self.assertNotIn("Migration", prod_tg.sent[-1]["text"])

                daemon.handle_update(
                    {
                        "update_id": 3,
                        "message": {
                            "message_id": 205,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "/status",
                        },
                    },
                    snapshots,
                    bot_role="default",
                )
                self.assertIn("<b>Herdr Telegram Bridge</b>", dev_tg.sent[-1]["text"])
                self.assertNotIn("Herdr Dev Agents", dev_tg.sent[-1]["text"])
                self.assertIn("Upgrade", dev_tg.sent[-1]["text"])
                self.assertIn("Migration", dev_tg.sent[-1]["text"])
                self.assertNotIn("Prod Support", dev_tg.sent[-1]["text"])

                daemon.handle_update(
                    {
                        "update_id": 53,
                        "message": {
                            "message_id": 206,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "/status all",
                        },
                    },
                    snapshots,
                    bot_role="prod",
                )
                self.assertIn("<b>Herdr Agents</b>", prod_tg.sent[-1]["text"])
                self.assertIn("Prod Support", prod_tg.sent[-1]["text"])
                self.assertIn("Migration", prod_tg.sent[-1]["text"])

                # Persisted state reloads prod fields cleanly
                reloaded = BridgeState.load(state_path)
                self.assertEqual(reloaded.last_update_id, 1)
                self.assertEqual(reloaded.prod_last_update_id, 50)
                self.assertEqual(reloaded.get_message_targets("default")["101"], "w22:p1")
                self.assertEqual(reloaded.get_message_targets("prod")["101"], "w21:p1")

                # Generic env secondary bot + user-customizable titles
                gen_env = Path(tmp) / "generic.env"
                gen_env.write_text(
                    "TELEGRAM_BOT_TOKEN=111:def\nTELEGRAM_CHAT_ID=777\nTELEGRAM_SECONDARY_BOT_TOKEN=222:sec\n",
                    encoding="utf-8",
                )
                gen_cfg = load_bridge_config(Path(tmp) / "missing.toml", env_path=gen_env, environ={})
                self.assertEqual(gen_cfg.bots["default"].display_title, "Herdr Telegram Bridge")
                self.assertEqual(gen_cfg.bots["secondary"].display_title, "Herdr Bridge (secondary)")

                renamed_env = Path(tmp) / "renamed.env"
                renamed_env.write_text(
                    "TELEGRAM_BOT_TOKEN=111:def\n"
                    "TELEGRAM_CHAT_ID=777\n"
                    "TELEGRAM_BOT_TITLE=Jun Coding Bot\n"
                    "TELEGRAM_SECONDARY_BOT_TOKEN=222:sec\n"
                    "TELEGRAM_SECONDARY_BOT_NAME=alerts\n"
                    "TELEGRAM_SECONDARY_BOT_TITLE=Pager Bot\n",
                    encoding="utf-8",
                )
                renamed_cfg = load_bridge_config(Path(tmp) / "missing.toml", env_path=renamed_env, environ={})
                self.assertEqual(renamed_cfg.bots["default"].display_title, "Jun Coding Bot")
                self.assertEqual(renamed_cfg.bots["alerts"].display_title, "Pager Bot")
            finally:
                daemon.close()

    def test_telegram_send_cli_auto_prod_default_and_single_bot_fallback(self) -> None:
        cli_path = Path(__file__).resolve().parent / "telegram-send"
        loader = importlib.machinery.SourceFileLoader("telegram_send_cli", str(cli_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        tg_send_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(tg_send_mod)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            env_file = tmp_dir / "env"
            state_dir = tmp_dir / "state"
            herdr = FakeHerdr(
                {
                    "w21:p1": make_snap(
                        pane_id="w21:p1",
                        workspace_id="w21",
                        workspace_label="Prod Support",
                        agent="agy",
                    ),
                    "w22:p1": make_snap(
                        pane_id="w22:p1",
                        workspace_id="w22",
                        workspace_label="Upgrade",
                        agent="agy",
                    ),
                }
            )
            herdr.footer_outputs["w21:p1"] = "Ctx 15% │ Gemini 2.5 Pro medium │ main"
            herdr.footer_outputs["w22:p1"] = "Ctx 22% │ Gemini 2.5 Pro medium │ main"

            clients_by_token: dict[str, FakeTelegram] = {
                "tok-dev": FakeTelegram(),
                "tok-prod": FakeTelegram(),
            }

            # 1. Dual-bot configured: auto routes w21:p1 -> tok-prod and w22:p1 -> tok-dev
            env_file.write_text(
                "TELEGRAM_BOT_TOKEN=tok-dev\nTELEGRAM_CHAT_ID=777\nTELEGRAM_PROD_BOT_TOKEN=tok-prod\n",
                encoding="utf-8",
            )
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                rc1 = tg_send_mod.run_cli(
                    ["--pane", "w21:p1", "🟢 HTTP sweep passed"],
                    env_path=env_file,
                    herdr=herdr,
                    client_factory=lambda tok: clients_by_token[tok],
                    state_dir=state_dir,
                )
                rc2 = tg_send_mod.run_cli(
                    ["--pane", "w22:p1", "Migration tests green"],
                    env_path=env_file,
                    herdr=herdr,
                    client_factory=lambda tok: clients_by_token[tok],
                    state_dir=state_dir,
                )
            self.assertEqual((rc1, rc2), (0, 0))
            self.assertEqual(len(clients_by_token["tok-prod"].sent), 1)
            self.assertEqual(len(clients_by_token["tok-dev"].sent), 1)
            self.assertIn("💬 <b>Prod Support (🤖 Pro)</b>", clients_by_token["tok-prod"].sent[0]["text"])
            self.assertIn("💬 <b>Upgrade (🤖 Pro)</b>", clients_by_token["tok-dev"].sent[0]["text"])

            # Both proactive sends got message_id=101 on their respective bots; daemon ingests without collision
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=clients_by_token["tok-dev"],  # type: ignore[arg-type]
                chat_id="777",
                state_path=state_dir / "state.json",
                summarizer=AgyFlashSummarizer(model="off"),
                prod_telegram=clients_by_token["tok-prod"],  # type: ignore[arg-type]
                prod_chat_id="777",
            )
            try:
                snapshots = daemon.poll_herdr_once()
                daemon.handle_update(
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 300,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "check logs on fleet",
                            "reply_to_message": {"message_id": 101, "text": "proactive"},
                        },
                    },
                    snapshots,
                    bot_role="prod",
                )
                self.assertEqual(herdr.prompts[-1], ("w21:p1", "check logs on fleet"))

                daemon.handle_update(
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 301,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "merge PR",
                            "reply_to_message": {"message_id": 101, "text": "proactive"},
                        },
                    },
                    snapshots,
                    bot_role="default",
                )
                self.assertEqual(herdr.prompts[-1], ("w22:p1", "merge PR"))
            finally:
                daemon.close()

            # 2. Explicit --prod and --bot default overrides work in dual-bot mode
            before_prod = len(clients_by_token["tok-prod"].sent)
            before_dev = len(clients_by_token["tok-dev"].sent)
            with contextlib.redirect_stdout(io.StringIO()):
                rc_force_prod = tg_send_mod.run_cli(
                    ["--prod", "--pane", "w22:p1", "Forced onto prod bot"],
                    env_path=env_file,
                    herdr=herdr,
                    client_factory=lambda tok: clients_by_token[tok],
                    state_dir=state_dir,
                )
                rc_force_dev = tg_send_mod.run_cli(
                    ["--bot", "default", "--pane", "w21:p1", "Forced onto dev bot"],
                    env_path=env_file,
                    herdr=herdr,
                    client_factory=lambda tok: clients_by_token[tok],
                    state_dir=state_dir,
                )
            self.assertEqual((rc_force_prod, rc_force_dev), (0, 0))
            self.assertEqual(len(clients_by_token["tok-prod"].sent), before_prod + 1)
            self.assertEqual(len(clients_by_token["tok-dev"].sent), before_dev + 1)

            # 3. Single-bot config (TELEGRAM_PROD_BOT_TOKEN unset): auto falls back cleanly to tok-dev, --prod errors
            before_fallback = len(clients_by_token["tok-dev"].sent)
            env_file.write_text("TELEGRAM_BOT_TOKEN=tok-dev\nTELEGRAM_CHAT_ID=777\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                rc_fallback = tg_send_mod.run_cli(
                    ["--pane", "w21:p1", "Fallback to single bot"],
                    env_path=env_file,
                    herdr=herdr,
                    client_factory=lambda tok: clients_by_token[tok],
                    state_dir=state_dir,
                )
            self.assertEqual(rc_fallback, 0)
            self.assertEqual(len(clients_by_token["tok-dev"].sent), before_fallback + 1)
            # In single-bot mode, 2 pro panes share one channel so w21:p1 gets pro1
            self.assertIn("💬 <b>Prod Support (🤖 Pro1)</b>", clients_by_token["tok-dev"].sent[-1]["text"])

            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc_err = tg_send_mod.run_cli(
                    ["--prod", "--pane", "w21:p1", "Should fail cleanly"],
                    env_path=env_file,
                    herdr=herdr,
                    client_factory=lambda tok: clients_by_token[tok],
                    state_dir=state_dir,
                )
            self.assertEqual(rc_err, 1)
            self.assertIn("TELEGRAM_PROD_BOT_TOKEN", err_buf.getvalue())

    def test_cowork_support_never_inflates_lead_model_number_and_display_handles(self) -> None:
        self.assertEqual(format_display_handle("sonnet"), "Sonnet")
        self.assertEqual(format_display_handle("flash2"), "Flash2")
        self.assertEqual(format_display_handle("gpt5-mini"), "GPT5-mini")
        self.assertEqual(format_display_handle("o3"), "O3")

        snaps = {
            "w15:p2": make_snap(pane_id="w15:p2", workspace_id="w15", name="cx-w15-p1", agent="codex"),
            "w24:p2": make_snap(pane_id="w24:p2", workspace_id="w24", name="cx-support-w24-p1", agent="codex"),
            "w30:p1": make_snap(pane_id="w30:p1", workspace_id="w30", workspace_label="Admin", agent="codex"),
        }
        models = {"w15:p2": "sonnet", "w24:p2": "sonnet", "w30:p1": "sonnet"}
        enriched = assign_model_handles(snaps, models)
        # Lone lead pane gets unnumbered 'sonnet' -> 'Admin (🤖 Sonnet)', while background helpers get sonnet2/sonnet3
        self.assertEqual(enriched["w30:p1"].friendly_handle, "sonnet")
        self.assertEqual(enriched["w30:p1"].header_label, "Admin (🤖 Sonnet)")
        self.assertEqual(enriched["w15:p2"].friendly_handle, "sonnet2")
        self.assertEqual(enriched["w24:p2"].friendly_handle, "sonnet3")

    def test_agy_divider_prompt_cron_wakeup_slicing_timer_deferral_and_silent_prod_green(self) -> None:
        # 1. Agy '────\n> prompt' with multi-paragraph paste slices cleanly right at model entry thought
        agy_interactive = (
            "  Older assistant reply that must be sliced away.\n\n"
            "────────────────────────────────────────────────────────────\n"
            "> Here is a multi-paragraph example:\n"
            "What was done\n\n"
            "Checks passed\n"
            "🟢 old check\n\n"
            "○ Thought for 12.4s (Analyzing user example)\n"
            "● Explored 1 file (herdr_telegram.py)\n"
            "… 3 more actions (1 read, 2 commands) …\n"
            "▸ Thought for 4s, 98 tokens\n\n"
            "  Updated the digest layout so section labels are omitted.\n"
            "────────────────────────────────────────────────────────────\n"
            ">\n"
            "────────────────────────────────────────────────────────────\n"
            "Ctx 14% │ Gemini 2.5 Pro medium\n"
        )
        extracted_interactive = extract_last_agent_turn(agy_interactive, agent_kind="agy")
        self.assertEqual(extracted_interactive, "Updated the digest layout so section labels are omitted.")

        # 2. Promptless cron/timer/task scrollback slices ONLY the latest wakeup instead of 7 hours of sweeps
        cron_scrollback = (
            "○ Thought for 2m 42s (Sweep #32 check)\n"
            "● Called 3 MCP tools (monitoring/http_check)\n"
            "  Sweep #32 (20:49 ET) is complete and all 12 hosts passed.\n\n"
            "○ Thought for 42.3s (Sweep #33 initial check)\n"
            "● Called 3 MCP tools (monitoring/alert_feed)\n"
            "● Explored 1 file (output.txt)\n"
            "  Sweep #33 (21:48 ET) initial checks passed; waiting on the parallel 12-host SSH sweep (task-2883).\n\n"
            "○ Thought for 19.3s (Every check completed)\n"
            "● Listed tasks\n"
            "● Ran (python3 -c 'send report') (ctrl+o to expand)\n\n"
            "  Sweep #33 (21:49 ET) is complete and all 12 production hosts passed HTTP and doctor checks.\n"
            "  Standing hourly cron task-396 remains active for Sweep #34.\n"
            "────────────────────────────────────────────────────────────\n"
            ">\n"
            "────────────────────────────────────────────────────────────\n"
            "  ● [14:20:36] cron (every hour) running\n"
            "Ctx 20% │ Gemini 2.5 Pro medium\n"
        )
        extracted_cron = extract_last_agent_turn(cron_scrollback, agent_kind="agy")
        self.assertIn("Sweep #33 (21:49 ET) is complete", extracted_cron)
        self.assertNotIn("Sweep #32", extracted_cron)
        self.assertNotIn("waiting on the parallel 12-host SSH sweep", extracted_cron)
        self.assertNotIn("Called 3 MCP tools", extracted_cron)
        # Completed sweep with 'cron task-396 remains active' is NOT deferred beyond 90s
        self.assertEqual(detect_mid_workflow_hold_seconds(cron_scrollback, 90.0), 90.0)

        # 3. Short healthcheck timer (<=120s) and background SSH sweep (task-2883) defer premature Done alerts,
        # whereas longer CI-check timers (e.g. 8-minute timer) send the 'currently undergoing checks' digest at 90s!
        timer_turn = (
            "○ Thought for 9.3s (Healthcheck after deploy)\n"
            "● Scheduled 1 timer (120s: Re-check api health after restart)\n\n"
            "  Restarted api; waiting 120s to confirm systemd health.\n"
        )
        self.assertEqual(detect_mid_workflow_hold_seconds(timer_turn, 90.0), 180.0)

        ci_timer_turn = (
            "○ Thought for 9.3s (CI running on PR #142)\n"
            "● Scheduled 1 timer (480s: Confirm CI and review pass before merge)\n\n"
            "  Opened acme/api#142 and started an 8-minute timer to confirm CI passes before merge.\n"
        )
        self.assertEqual(detect_mid_workflow_hold_seconds(ci_timer_turn, 90.0), 90.0)

        bg_wait_turn = (
            "○ Thought for 41.1s (Initial sweep)\n"
            "● Ran (python3 sweep.py)\n\n"
            "  Sweep #29 initial checks are clean; waiting on the parallel 12-host SSH doctor sweep (task-2813).\n"
        )
        self.assertEqual(detect_mid_workflow_hold_seconds(bg_wait_turn, 90.0), 300.0)

        # End-to-end daemon check: mid-rollout timer does NOT fire at 95s, wakes at 240s, and final all-green
        # Prod Support sweep delivers ONCE with disable_notification=True (whereas P2 finding buzzes!)
        class StubGreenSummarizer(AgyFlashSummarizer):
            def __init__(self) -> None:
                super().__init__(model="gemini-2.5-flash", min_chars=20)
                self.next_findings: tuple[str, ...] = ()

            def summarize(self, cleaned_turn: str, **_: Any) -> DigestSections | None:
                return DigestSections(
                    done=("**Sweep #33:** All 12 hosts healthy on api v1.4.2.",),
                    checks=("HTTP sweep (12/12 hosts)", "Doctors & systemd (0 failed)"),
                    findings=self.next_findings,
                    attention=(),
                )

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr(
                {
                    "w21:p1": make_snap(
                        pane_id="w21:p1",
                        workspace_id="w21",
                        workspace_label="Prod Support",
                        agent="agy",
                        status="working",
                        state_change_seq=1,
                    )
                }
            )
            herdr.footer_outputs["w21:p1"] = "Ctx 20% │ Gemini 2.5 Pro medium"
            herdr.outputs["w21:p1"] = timer_turn
            dev_tg = FakeTelegram()
            prod_tg = FakeTelegram()
            stub_sum = StubGreenSummarizer()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=dev_tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
                summarizer=stub_sum,
                async_summaries=False,
                prod_telegram=prod_tg,  # type: ignore[arg-type]
                prod_chat_id="777",
            )
            try:
                daemon.poll_herdr_once()
                # Enters idle after scheduling 120s healthcheck timer -> NO Done text at 95s or 145s!
                herdr.snapshots["w21:p1"] = make_snap(
                    pane_id="w21:p1",
                    workspace_id="w21",
                    workspace_label="Prod Support",
                    agent="agy",
                    status="idle",
                    state_change_seq=2,
                )
                daemon.poll_herdr_once()
                clock.advance(95.0)
                daemon.poll_herdr_once()
                self.assertEqual(len(prod_tg.sent), 0)
                read_count_after_hold = len(herdr.read_calls)
                clock.advance(50.0)
                daemon.poll_herdr_once()
                # Cached hold_seconds avoids re-reading terminal on every 2s poll while held!
                self.assertEqual(len(herdr.read_calls), read_count_after_hold)
                self.assertEqual(len(prod_tg.sent), 0)
                prompt_text = AgyFlashSummarizer._build_prompt("sample")
                self.assertIn("currently undergoing checks before merging", prompt_text)
                self.assertIn("NEVER mention timers", prompt_text)

                # Timer fires -> agent works -> finishes final Patrol #33 -> delivers silently at +90s!
                clock.advance(15.0)
                herdr.snapshots["w21:p1"] = make_snap(
                    pane_id="w21:p1",
                    workspace_id="w21",
                    workspace_label="Prod Support",
                    agent="agy",
                    status="working",
                    state_change_seq=3,
                )
                daemon.poll_herdr_once()
                herdr.outputs["w21:p1"] = cron_scrollback
                herdr.snapshots["w21:p1"] = make_snap(
                    pane_id="w21:p1",
                    workspace_id="w21",
                    workspace_label="Prod Support",
                    agent="agy",
                    status="idle",
                    state_change_seq=4,
                )
                # Completed Prod Support patrol sends immediately on the very first poll (0s wait)!
                daemon.poll_herdr_once()
                self.assertEqual(len(prod_tg.sent), 1)
                self.assertTrue(prod_tg.sent[0]["disable_notification"])
                self.assertIn("Prod Support (🤖 Pro)", prod_tg.sent[0]["text"])

                # Next sweep has a [P2] finding -> sends immediately (0s wait) AND disable_notification is False!
                stub_sum.next_findings = ("[P2] metrics-worker memory at 84%",)
                herdr.snapshots["w21:p1"] = make_snap(
                    pane_id="w21:p1",
                    workspace_id="w21",
                    workspace_label="Prod Support",
                    agent="agy",
                    status="working",
                    state_change_seq=5,
                )
                daemon.poll_herdr_once()
                herdr.snapshots["w21:p1"] = make_snap(
                    pane_id="w21:p1",
                    workspace_id="w21",
                    workspace_label="Prod Support",
                    agent="agy",
                    status="idle",
                    state_change_seq=6,
                )
                daemon.poll_herdr_once()
                self.assertEqual(len(prod_tg.sent), 2)
                self.assertFalse(prod_tg.sent[1]["disable_notification"])
            finally:
                daemon.close()

    def test_immediate_telegram_reply_fast_track_and_pr_author_explanation_rules(self) -> None:
        prompt_text = AgyFlashSummarizer._build_prompt("sample")
        self.assertIn("by 🤖", prompt_text)
        self.assertIn("by @alex", prompt_text)
        self.assertIn("never has to scroll up", prompt_text)

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            herdr = FakeHerdr(
                {
                    "w22:p1": make_snap(
                        pane_id="w22:p1",
                        workspace_id="w22",
                        workspace_label="Upgrade",
                        agent="agy",
                        status="working",
                        state_change_seq=1,
                    ),
                    "w21:p1": make_snap(
                        pane_id="w21:p1",
                        workspace_id="w21",
                        workspace_label="Prod Support",
                        agent="agy",
                        status="idle",
                        state_change_seq=1,
                    ),
                }
            )
            herdr.footer_outputs["w22:p1"] = "Ctx 15% │ Gemini 2.5 Pro medium"
            herdr.footer_outputs["w21:p1"] = "Ctx 20% │ Gemini 2.5 Pro medium"
            herdr.outputs["w22:p1"] = "Initial laptop task finished."
            herdr.outputs["w21:p1"] = "Direct explanation on Prod Support."
            dev_tg = FakeTelegram()
            prod_tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=dev_tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
                summarizer=AgyFlashSummarizer(model="off"),
                async_summaries=False,
                prod_telegram=prod_tg,  # type: ignore[arg-type]
                prod_chat_id="777",
            )
            try:
                snapshots = daemon.poll_herdr_once()
                # 1. Unprompted dev workspace turn still waits 90s
                herdr.snapshots["w22:p1"] = make_snap(
                    pane_id="w22:p1",
                    workspace_id="w22",
                    workspace_label="Upgrade",
                    agent="agy",
                    status="idle",
                    state_change_seq=2,
                )
                daemon.poll_herdr_once()
                self.assertEqual(len(dev_tg.sent), 0)
                clock.advance(95.0)
                snapshots = daemon.poll_herdr_once()
                self.assertEqual(len(dev_tg.sent), 1)
                alert_msg_id = dev_tg.sent[0]["message_id"]

                # 2. User swipe-replies on Telegram asking for an explanation
                daemon.handle_update(
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 500,
                            "date": time.time(),
                            "chat": {"id": 777, "type": "private"},
                            "from": {"id": 777},
                            "text": "explain why",
                            "reply_to_message": {"message_id": alert_msg_id},
                        },
                    },
                    snapshots,
                    bot_role="default",
                )
                # Sent "➡️ Sent to Upgrade (🤖 Pro)." confirmation
                self.assertEqual(len(dev_tg.sent), 2)

                # Race guard: polling BEFORE herdr increments seq from 2 -> 3 does NOT re-send old turn
                daemon.poll_herdr_once()
                self.assertEqual(len(dev_tg.sent), 2)

                # Agent works (seq=3) and finishes (seq=4) -> skips 90s wait and sends at t=0s!
                herdr.outputs["w22:p1"] = "Here is the explanation you asked for."
                herdr.snapshots["w22:p1"] = make_snap(
                    pane_id="w22:p1",
                    workspace_id="w22",
                    workspace_label="Upgrade",
                    agent="agy",
                    status="working",
                    state_change_seq=3,
                )
                daemon.poll_herdr_once()
                herdr.snapshots["w22:p1"] = make_snap(
                    pane_id="w22:p1",
                    workspace_id="w22",
                    workspace_label="Upgrade",
                    agent="agy",
                    status="idle",
                    state_change_seq=4,
                )
                snapshots = daemon.poll_herdr_once()
                self.assertEqual(len(dev_tg.sent), 3)
                self.assertIn("Here is the explanation you asked for.", dev_tg.sent[-1]["text"])

                # 3. Sub-2s fast reply race (jumps straight idle seq=4 -> idle seq=6 between polls) also skips wait!
                daemon.handle_update(
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 501,
                            "date": time.time(),
                            "chat": {"id": 777, "type": "private"},
                            "from": {"id": 777},
                            "text": "pro: one more detail",
                        },
                    },
                    snapshots,
                    bot_role="default",
                )
                self.assertEqual(len(dev_tg.sent), 4)
                herdr.outputs["w22:p1"] = "Instant detail answer."
                herdr.snapshots["w22:p1"] = make_snap(
                    pane_id="w22:p1",
                    workspace_id="w22",
                    workspace_label="Upgrade",
                    agent="agy",
                    status="idle",
                    state_change_seq=6,
                )
                daemon.poll_herdr_once()
                self.assertEqual(len(dev_tg.sent), 5)
                self.assertIn("Instant detail answer.", dev_tg.sent[-1]["text"])

                # 4. Subsequent unprompted laptop turn goes back to normal 90s debounce
                herdr.outputs["w22:p1"] = "Later laptop edit."
                herdr.snapshots["w22:p1"] = make_snap(
                    pane_id="w22:p1",
                    workspace_id="w22",
                    workspace_label="Upgrade",
                    agent="agy",
                    status="working",
                    state_change_seq=7,
                )
                daemon.poll_herdr_once()
                herdr.snapshots["w22:p1"] = make_snap(
                    pane_id="w22:p1",
                    workspace_id="w22",
                    workspace_label="Upgrade",
                    agent="agy",
                    status="idle",
                    state_change_seq=8,
                )
                daemon.poll_herdr_once()
                self.assertEqual(len(dev_tg.sent), 5)
                clock.advance(95.0)
                daemon.poll_herdr_once()
                self.assertEqual(len(dev_tg.sent), 6)
            finally:
                daemon.close()

    def test_blocked_ask_user_question_and_bash_dialog_clean_formatting_and_buttons(self) -> None:
        claude_question = (
            "● Reading the auth code now: how email 2FA was added for standard users and\n"
            "  every way a password can be reset. Then I'll come back with a plan.\n\n"
            "  Ran 7 shell commands\n\n"
            "─────────────────────────────────────────────────────────────────────────────\n"
            " ☐ Reset route\n\n"
            "│ When an admin resets the password of another admin who uses email codes for\n"
            "│ 2FA, how should the new password reach them? An emailed reset link sends\n"
            "│ both the password and the code to the same mailbox, so someone who controls\n"
            "│ that mailbox gets both factors.\n\n"
            "❯ 1. Temp password only (Recommended)\n"
            "     For email-code admins, the console only offers a temporary password,\n"
            "     which the resetting admin passes on another way (call, Teams). Two\n"
            "     separate channels, so it's real 2FA.\n"
            "  2. Allow the email link\n"
            "     Same as everyone else. The other admin's approval is the only check;\n"
            "     someone who controls the mailbox ends up with both factors after a\n"
            "     reset.\n"
            "  3. Link + clear their 2FA\n"
            "     Send the reset link, and the admin must set up 2FA again at next\n"
            "     sign-in. Doesn't help if the mailbox is compromised, so it's weaker than\n"
            "     the temporary password.\n"
            "  4. Type something.\n"
            "─────────────────────────────────────────────────────────────────────────────\n"
            "  5. Chat about this\n\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        snap = dataclasses.replace(
            make_snap(
                pane_id="w25:p1",
                workspace_id="w25",
                workspace_label="~",
                agent="claude",
                status="blocked",
                title="Admin email 2FA and password reset",
                state_change_seq=7,
            ),
            model="opus",
            model_handle="opus4",
        )
        msg = format_transition_message("blocked", snap, claude_question)
        self.assertIn("⚠️ <b>Blocked</b> · <b>~ (🤖 Opus4)</b>", msg)
        self.assertIn("<i>Admin email 2FA and password reset</i>", msg)
        self.assertIn("<b>Reset route</b>", msg)
        self.assertIn("<b>1. Temp password only (Recommended)</b>", msg)
        self.assertNotIn("Ran 7 shell commands", msg)
        self.assertNotIn("Type something.", msg)
        self.assertNotIn("Chat about this", msg)
        self.assertNotIn("Enter to select", msg)
        self.assertNotIn("Swipe-reply", msg)
        self.assertNotIn("────", msg)

        kb = build_transition_keyboard(
            "blocked", "w25:p1", state_change_seq=7, dialog_text=claude_question, terminal_id="term_1"
        )
        all_rows = [[b["text"] for b in row] for row in kb["inline_keyboard"]]
        self.assertEqual(
            all_rows,
            [
                ["1. Temp password only (Recommended)"],
                ["2. Allow the email link"],
                ["3. Link + clear their 2FA"],
                ["💬 5. Chat about this"],
                ["📖 Read output"],
            ],
        )

        # Tapping '💬 5. Chat about this' sends key '5' and binds swipe-replies on the confirmation to w25:p1!
        with tempfile.TemporaryDirectory() as tmp:
            herdr = FakeHerdr({"w25:p1": snap})
            herdr.visible_outputs["w25:p1"] = claude_question
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                summarizer=AgyFlashSummarizer(model="off"),
                async_summaries=False,
            )
            try:
                chat_cb = kb["inline_keyboard"][3][0]["callback_data"]
                daemon.handle_update(
                    {
                        "update_id": 1,
                        "callback_query": {
                            "id": "cq_chat",
                            "from": {"id": 777},
                            "message": {"chat": {"id": 777, "type": "private"}},
                            "data": chat_cb,
                        },
                    },
                    {"w25:p1": snap},
                )
                self.assertEqual(herdr.keys, [("w25:p1", ["5"])])
                self.assertIn("Exited prompt", tg.sent[-1]["text"])
                confirm_msg_id = tg.sent[-1]["message_id"]
                self.assertEqual(daemon.state.get_message_targets("default")[str(confirm_msg_id)], "w25:p1")
            finally:
                daemon.close()

        # Bash command approval dialog preserves <pre> command block cleanly
        bash_dialog = (
            "─────────────────────────────────────────────────────────────────────────────\n"
            " Bash command\n\n"
            "   git push origin fix/admin-email-2fa\n"
            "   Push branch to remote\n\n"
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. Yes, and don't ask again for git push commands\n"
            "   3. No\n"
            "Esc to cancel · Tab to amend\n"
        )
        bash_msg = format_transition_message("blocked", snap, bash_dialog)
        self.assertIn("<b>Bash command</b>", bash_msg)
        self.assertIn("<pre>git push origin fix/admin-email-2fa\nPush branch to remote</pre>", bash_msg)
        self.assertNotIn("Esc to cancel", bash_msg)

    def test_turn_summarizer_multi_provider_cli_http_auto_and_security(self) -> None:
        long_turn = (
            "Implemented OAuth token refresh and verified API suite with sk-secret1234567890abcdef.\n" * 6
        ).strip()

        # 1. Auto-resolution priority: provider:model shorthand (preserving colons inside ollama model tags),
        #    CLI detection order (claude -> codex -> gemini -> agy), API key fallback, and clean 'off' fallback.
        self.assertEqual(
            resolve_summary_backend("auto", "ollama:qwen2.5:7b", env={}, which_fn=lambda _: None)[:2],
            ("ollama", "qwen2.5:7b"),
        )
        self.assertEqual(
            resolve_summary_backend(
                "auto",
                "",
                env={},
                which_fn=lambda name: f"/opt/bin/{name}" if name in ("codex", "gemini") else None,
            )[:3],
            ("codex", "", "/opt/bin/codex"),
        )
        self.assertEqual(
            resolve_summary_backend(
                "auto",
                "",
                env={"ANTHROPIC_API_KEY": "ant-test-key"},
                which_fn=lambda _: None,
            )[:2],
            ("anthropic", "claude-haiku-4-5"),
        )
        self.assertEqual(
            resolve_summary_backend(
                "auto",
                "gpt-4.1-mini",
                env={"OPENAI_API_KEY": "oa-test-key"},
                which_fn=lambda _: "/usr/bin/claude",
            )[:2],
            ("openai", "gpt-4.1-mini"),
        )
        off_sum = TurnSummarizer(provider="auto", model="", env={}, which_fn=lambda _: None)
        self.assertFalse(off_sum.enabled)
        self.assertEqual(off_sum.summary_label, "off")
        self.assertIsNone(off_sum.summarize(long_turn))

        # 2. Non-loopback cleartext HTTP URLs are rejected to protect API keys, while loopback Ollama HTTP works
        insecure = TurnSummarizer(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="oa-key",
            api_url="http://llm.example.com/v1",
            env={},
            which_fn=lambda _: None,
        )
        self.assertFalse(insecure.enabled)

        # 3. Claude CLI provider: no --bare (preserves OAuth login), stdin prompt, --tools "", scrubbed env
        claude_call: dict[str, Any] = {}

        def fake_claude_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            claude_call["cmd"] = cmd
            claude_call["kwargs"] = kwargs
            out = {"result": json.dumps({"done": ["**OAuth refresh:** Updated token handler."], "checks": ["pytest"]})}
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=json.dumps(out), stderr="")

        claude_sum = TurnSummarizer(
            provider="claude",
            model="",
            min_chars=40,
            binary="/usr/local/bin/claude",
            env={},
            which_fn=lambda _: None,
        )
        self.assertEqual(claude_sum.summary_label, "claude:haiku")
        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "999999999:AAHsecretbot_token_value_1234567890",
                "ANTHROPIC_API_KEY": "sk-ant-allowed-for-claude",
            },
        ):
            with patch("subprocess.run", side_effect=fake_claude_run):
                c_digest = claude_sum.summarize(long_turn, extra_style="Use short bullets.")
        self.assertIsNotNone(c_digest)
        assert c_digest is not None
        self.assertEqual(c_digest.checks, ("pytest",))
        self.assertNotIn("--bare", claude_call["cmd"])
        self.assertIn("-p", claude_call["cmd"])
        self.assertEqual(claude_call["cmd"][claude_call["cmd"].index("--setting-sources") + 1], "project")
        self.assertIn("--no-session-persistence", claude_call["cmd"])
        self.assertIn("--strict-mcp-config", claude_call["cmd"])
        tools_idx = claude_call["cmd"].index("--tools")
        self.assertEqual(claude_call["cmd"][tools_idx + 1], "")
        self.assertNotIn("<untrusted_terminal_output>", claude_call["cmd"][-1])
        self.assertIn("<untrusted_terminal_output>", claude_call["kwargs"]["input"])
        self.assertIn("Use short bullets.", claude_call["kwargs"]["input"])
        self.assertNotIn("sk-secret1234567890abcdef", claude_call["kwargs"]["input"])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", claude_call["kwargs"]["env"])
        self.assertEqual(claude_call["kwargs"]["env"].get("ANTHROPIC_API_KEY"), "sk-ant-allowed-for-claude")

        # 4. Codex CLI provider: ephemeral read-only sandbox reading -o output file and passing prompt via stdin
        codex_call: dict[str, Any] = {}

        def fake_codex_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            codex_call["cmd"] = cmd
            codex_call["input"] = kwargs.get("input", "")
            out_idx = cmd.index("-o")
            Path(cmd[out_idx + 1]).write_text(
                json.dumps({"done": ["**Codex digest:** Verified suite."], "checks": ["ruff & unittest"]}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        codex_sum = TurnSummarizer(
            provider="codex",
            model="o4-mini",
            min_chars=40,
            binary="/usr/local/bin/codex",
            env={},
            which_fn=lambda _: None,
        )
        with patch("subprocess.run", side_effect=fake_codex_run):
            cx_digest = codex_sum.summarize(long_turn)
        self.assertIsNotNone(cx_digest)
        self.assertIn("--ephemeral", codex_call["cmd"])
        self.assertIn("read-only", codex_call["cmd"])
        self.assertEqual(codex_call["cmd"][-1], "-")
        self.assertIn("<untrusted_terminal_output>", codex_call["input"])

        # 5. Gemini CLI provider: read-only --approval-mode plan without Docker --sandbox, stdin prompt
        gemini_call: dict[str, Any] = {}

        def fake_gemini_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            gemini_call["cmd"] = cmd
            gemini_call["kwargs"] = kwargs
            out = {"response": json.dumps({"done": ["**Gemini digest:** Passed checks."]})}
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=json.dumps(out), stderr="")

        gem_sum = TurnSummarizer(
            provider="gemini",
            model="",
            min_chars=40,
            binary="/usr/local/bin/gemini",
            env={},
            which_fn=lambda _: None,
        )
        with patch("subprocess.run", side_effect=fake_gemini_run):
            g_digest = gem_sum.summarize(long_turn)
        self.assertIsNotNone(g_digest)
        self.assertNotIn("--sandbox", gemini_call["cmd"])
        self.assertEqual(gemini_call["cmd"][gemini_call["cmd"].index("--approval-mode") + 1], "plan")
        self.assertIn("--admin-policy", gemini_call["cmd"])
        self.assertIn("<untrusted_terminal_output>", gemini_call["kwargs"]["input"])

        # 6. HTTP providers: OpenAI, Ollama (loopback HTTP), Anthropic, Gemini REST API, and redirect blocking
        http_requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

        class FakeHTTPResponse:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._raw = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self._raw

            def __enter__(self) -> FakeHTTPResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def make_urlopen(resp_payload: dict[str, Any]) -> Any:
            def _urlopen(req: Any, timeout: float = 0.0) -> FakeHTTPResponse:
                del timeout
                hdrs = {k.lower(): v for k, v in req.header_items()}
                body = json.loads(req.data.decode("utf-8"))
                http_requests.append((req.full_url, hdrs, body))
                return FakeHTTPResponse(resp_payload)

            return _urlopen

        digest_str = json.dumps({"done": ["**HTTP summary:** Clean rollout."], "checks": ["unit tests"]})

        oa_sum = TurnSummarizer(provider="openai", api_key="oa-secret", min_chars=40, env={}, which_fn=lambda _: None)
        with patch(
            "herdr_telegram._open_summary_http",
            side_effect=make_urlopen({"choices": [{"message": {"content": digest_str}}]}),
        ):
            oa_res = oa_sum.summarize(long_turn)
        self.assertIsNotNone(oa_res)
        self.assertEqual(http_requests[-1][0], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(http_requests[-1][1]["authorization"], "Bearer oa-secret")
        self.assertNotIn("sk-secret1234567890abcdef", http_requests[-1][2]["messages"][0]["content"])

        ol_sum = TurnSummarizer(provider="ollama", model="qwen2.5:7b", min_chars=40, env={}, which_fn=lambda _: None)
        with patch(
            "herdr_telegram._open_summary_http",
            side_effect=make_urlopen({"choices": [{"message": {"content": digest_str}}]}),
        ):
            ol_res = ol_sum.summarize(long_turn)
        self.assertIsNotNone(ol_res)
        self.assertEqual(http_requests[-1][0], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(http_requests[-1][2]["model"], "qwen2.5:7b")

        an_sum = TurnSummarizer(
            provider="anthropic", api_key="ant-secret", min_chars=40, env={}, which_fn=lambda _: None
        )
        with patch(
            "herdr_telegram._open_summary_http",
            side_effect=make_urlopen({"content": [{"type": "text", "text": digest_str}]}),
        ):
            an_res = an_sum.summarize(long_turn)
        self.assertIsNotNone(an_res)
        self.assertEqual(http_requests[-1][0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(http_requests[-1][1]["x-api-key"], "ant-secret")

        ga_sum = TurnSummarizer(
            provider="gemini-api", api_key="gem-secret", min_chars=40, env={}, which_fn=lambda _: None
        )
        with patch(
            "herdr_telegram._open_summary_http",
            side_effect=make_urlopen({"candidates": [{"content": {"parts": [{"text": digest_str}]}}]}),
        ):
            ga_res = ga_sum.summarize(long_turn)
        self.assertIsNotNone(ga_res)
        self.assertIn(
            "generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent", http_requests[-1][0]
        )
        self.assertNotIn("gem-secret", http_requests[-1][0])
        self.assertEqual(http_requests[-1][1]["x-goog-api-key"], "gem-secret")

        # Real loopback HTTP 302 redirect is blocked rather than followed to a remote host
        redirect_hits: list[str] = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                redirect_hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://198.51.100.10/collect")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                del format, args

        server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            redir_sum = TurnSummarizer(
                provider="ollama",
                model="qwen2.5:7b",
                api_url=f"http://127.0.0.1:{server.server_port}/v1",
                min_chars=40,
                env={},
                which_fn=lambda _: None,
            )
            self.assertIsNone(redir_sum.summarize(long_turn))
            self.assertEqual(redirect_hits, ["/v1/chat/completions"])
        finally:
            server.server_close()
            thread.join(timeout=2.0)

    def test_install_script_local_symlink_and_remote_clone_modes(self) -> None:
        repo_root = Path(__file__).resolve().parent
        install_script = repo_root / "install.sh"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # 1. Local checkout mode: symlinks INSTALL_DIR -> repo_root and creates 0600 env without clobbering
            local_home = base / "local_home"
            custom_cfg_dir = local_home / "config" / "herdr-telegram"
            custom_install_dir = local_home / "share" / "herdr-telegram"
            local_env = {
                "HOME": str(local_home),
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HERDR_TELEGRAM_OS_OVERRIDE": "Linux",
                "HERDR_TELEGRAM_INSTALL_DIR": str(custom_install_dir),
                "HERDR_TELEGRAM_BIN_DIR": str(local_home / "bin"),
                "HERDR_TELEGRAM_CONFIG_DIR": str(custom_cfg_dir),
                "HERDR_TELEGRAM_SYSTEMD_DIR": str(local_home / "systemd"),
                "HERDR_TELEGRAM_SKIP_SYSTEMCTL": "1",
            }
            res_local = subprocess.run(
                ["bash", str(install_script)],
                env=local_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_local.returncode, 0, res_local.stderr)
            self.assertTrue(custom_install_dir.is_symlink())
            self.assertEqual(custom_install_dir.resolve(), repo_root)
            self.assertTrue((local_home / "bin" / "telegram-send").is_symlink())
            self.assertTrue((local_home / "bin" / "ts-preview").is_symlink())
            service_unit = local_home / "systemd" / "herdr-telegram.service"
            self.assertTrue(service_unit.is_file())
            unit_text = service_unit.read_text(encoding="utf-8")
            self.assertIn(str(custom_install_dir), unit_text)
            self.assertIn(str(custom_cfg_dir), unit_text)
            self.assertTrue((custom_cfg_dir / "config.example.toml").is_file())
            cfg_env = custom_cfg_dir / "env"
            self.assertTrue(cfg_env.is_file())
            self.assertEqual(cfg_env.stat().st_mode & 0o777, 0o600)

            cfg_env.write_text("TELEGRAM_BOT_TOKEN=preserve-me\n", encoding="utf-8")
            subprocess.run(["bash", str(install_script)], env=local_env, check=True, capture_output=True)
            self.assertEqual(cfg_env.read_text(encoding="utf-8"), "TELEGRAM_BOT_TOKEN=preserve-me\n")

            # Refuses unsafe HERDR_TELEGRAM_INSTALL_DIR=$HOME and refuses clobbering unrelated non-empty dir
            bad_home_env = dict(local_env, HERDR_TELEGRAM_INSTALL_DIR=str(local_home))
            res_bad_home = subprocess.run(
                ["bash", str(install_script)],
                env=bad_home_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(res_bad_home.returncode, 0)

            unrelated_dir = base / "unrelated_files"
            unrelated_dir.mkdir()
            sentinel = unrelated_dir / "keep.txt"
            sentinel.write_text("important\n", encoding="utf-8")
            bad_dir_env = dict(local_env, HERDR_TELEGRAM_INSTALL_DIR=str(unrelated_dir))
            res_bad_dir = subprocess.run(
                ["bash", str(install_script)],
                env=bad_dir_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(res_bad_dir.returncode, 0)
            self.assertTrue(sentinel.is_file())

            # 2. One-line piped install mode (`curl ... | bash`): clones repo and updates cleanly on re-run
            origin_repo = base / "origin_repo"
            origin_repo.mkdir()
            shutil.copytree(repo_root / "bridge", origin_repo / "bridge")
            for fname in (
                "herdr_telegram.py",
                "herdr-telegram",
                "telegram-send",
                "telegram_send.py",
                "ts-preview",
                "ts_preview.py",
                "herdr-telegram.service",
                "env.example",
                "config.example.toml",
                "install.sh",
            ):
                (origin_repo / fname).write_bytes((repo_root / fname).read_bytes())
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin_repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=origin_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=origin_repo, check=True)
            subprocess.run(["git", "add", "."], cwd=origin_repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=origin_repo, check=True)

            remote_home = base / "remote_home"
            remote_env = {
                "HOME": str(remote_home),
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HERDR_TELEGRAM_OS_OVERRIDE": "Linux",
                "HERDR_TELEGRAM_REPO_URL": f"file://{origin_repo}",
                "HERDR_TELEGRAM_INSTALL_DIR": str(remote_home / "share" / "herdr-telegram"),
                "HERDR_TELEGRAM_BIN_DIR": str(remote_home / "bin"),
                "HERDR_TELEGRAM_CONFIG_DIR": str(remote_home / "config" / "herdr-telegram"),
                "HERDR_TELEGRAM_SYSTEMD_DIR": str(remote_home / "systemd"),
                "HERDR_TELEGRAM_SKIP_SYSTEMCTL": "1",
            }
            script_body = install_script.read_text(encoding="utf-8")
            res_pipe = subprocess.run(
                ["bash", "-s"],
                input=script_body,
                env=remote_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_pipe.returncode, 0, res_pipe.stderr)
            self.assertTrue((remote_home / "share" / "herdr-telegram" / ".git").is_dir())
            self.assertTrue((remote_home / "share" / "herdr-telegram" / "herdr_telegram.py").is_file())
            self.assertTrue((remote_home / "share" / "herdr-telegram" / "bridge" / "__init__.py").is_file())

            # Re-running one-liner pulls fast-forward updates cleanly
            res_update = subprocess.run(
                ["bash", "-s"],
                input=script_body,
                env=remote_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_update.returncode, 0, res_update.stderr)
            self.assertIn("Updating existing installation", res_update.stdout)

    def test_modular_config_toml_rule_engine_multi_bot_and_telegram_send_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            cfg_dir.mkdir()
            env_path = cfg_dir / "env"
            toml_path = cfg_dir / "config.toml"
            env_path.write_text(
                "export TELEGRAM_BOT_TOKEN=111:default-tok # inline comment\n"
                "OPS_BOT_TOKEN='222:ops-tok#literal' # trailing comment\n"
                'NIGHTLY_BOT_TOKEN="333:nightly-tok" # another comment\n'
                "TELEGRAM_CHAT_ID=777\n",
                encoding="utf-8",
            )
            parsed_env = parse_env_file(env_path)
            self.assertEqual(parsed_env["TELEGRAM_BOT_TOKEN"], "111:default-tok")
            self.assertEqual(parsed_env["OPS_BOT_TOKEN"], "222:ops-tok#literal")
            self.assertEqual(parsed_env["NIGHTLY_BOT_TOKEN"], "333:nightly-tok")

            toml_path.write_text(
                "[bridge]\n"
                "poll_interval = 1.5\n"
                "summary_min_chars = 0\n"
                'spawn_roots = ["~/work", "~/oss"]\n\n'
                "[bots.ops]\n"
                'token_env = "OPS_BOT_TOKEN"\n'
                'title = "Ops Alerts"\n\n'
                "[bots.nightly]\n"
                'token_env = "NIGHTLY_BOT_TOKEN"\n'
                'title = "Nightly Audits"\n\n'
                "[defaults]\n"
                'bot = "default"\n'
                "idle_delay_seconds = 90\n"
                "blocked_delay_seconds = 30\n"
                "silent_when_green = false\n\n"
                "[[routes]]\n"
                'name = "ops-oncall"\n'
                'match_workspaces = ["ops-*", "oncall", "incident-response"]\n'
                'bot = "ops"\n'
                "idle_delay_seconds = 0\n"
                "silent_when_green = true\n"
                'summary_style = "Group checks per cluster."\n\n'
                "[[routes]]\n"
                'name = "nightly-audits"\n'
                'match_agents = ["opus-audit"]\n'
                'bot = "nightly"\n'
                "idle_delay_seconds = 0\n\n"
                "[agents.opus-audit]\n"
                'kind = "claude"\n'
                'spawn_args = ["--model", "opus"]\n'
                'model_patterns = ["model: (\\\\S+)"]\n'
                'default_model = "opus"\n',
                encoding="utf-8",
            )
            bridge_cfg = load_bridge_config(toml_path, env_path=env_path, environ={})
            self.assertEqual(bridge_cfg.summary_min_chars, 0)
            opus_spec = bridge_cfg.agent_registry.get("opus-audit")
            assert opus_spec is not None
            self.assertEqual(opus_spec.model_patterns[0], r"model: (\S+)")
            self.assertTrue(match_glob_token("Ops", ["ops-*"]))
            self.assertTrue(match_glob_token("Ops Cluster", ["ops-*"]))
            self.assertFalse(match_glob_token("opsec", ["ops-*"]))
            self.assertEqual(set(bridge_cfg.bots.keys()), {"default", "ops", "nightly"})
            self.assertEqual(bridge_cfg.bots["ops"].token, "222:ops-tok#literal")

            # Legacy .env-only 1-bot parity: Prod Support gets 0s wait and buzzes normally (silent_when_green=False)
            legacy_env = cfg_dir / "legacy_env"
            legacy_env.write_text("TELEGRAM_BOT_TOKEN=111:only\nTELEGRAM_CHAT_ID=777\n", encoding="utf-8")
            legacy_cfg = load_bridge_config(cfg_dir / "missing.toml", env_path=legacy_env, environ={})
            legacy_prod_route = legacy_cfg.rule_engine.resolve(
                make_snap(workspace_label="Prod Support"), available_bots=legacy_cfg.bots.keys()
            )
            self.assertEqual(legacy_prod_route.idle_delay_seconds, 0.0)
            self.assertFalse(legacy_prod_route.silent_when_green)

            # Verify multi-bot routing in BridgeDaemon (default vs ops vs nightly)
            clock = FakeClock()
            herdr = FakeHerdr(
                {
                    "w1:p1": make_snap(
                        pane_id="w1:p1",
                        workspace_id="w1",
                        workspace_label="Oncall",
                        agent="agy",
                        status="working",
                        state_change_seq=1,
                    ),
                    "w2:p1": make_snap(
                        pane_id="w2:p1",
                        workspace_id="w2",
                        workspace_label="SecurityRepo",
                        agent="claude",
                        name="opus-audit-w2-p1",
                        status="working",
                        state_change_seq=1,
                    ),
                    "w3:p1": make_snap(
                        pane_id="w3:p1",
                        workspace_id="w3",
                        workspace_label="FeatureWork",
                        agent="agy",
                        status="working",
                        state_change_seq=1,
                    ),
                }
            )
            herdr.footer_outputs["w1:p1"] = "Ctx 20% │ Gemini 2.5 Pro"
            herdr.footer_outputs["w3:p1"] = "Ctx 20% │ Gemini 2.5 Pro"
            herdr.outputs["w1:p1"] = "All cluster checks healthy across 8 services and 0 errors."
            herdr.outputs["w2:p1"] = "Completed nightly dependency audit with zero critical CVEs."
            herdr.outputs["w3:p1"] = "Finished feature refactor and unit tests."

            seen_styles: list[str] = []

            class StyleSpySummarizer(TurnSummarizer):
                def __init__(self) -> None:
                    super().__init__(provider="agy", model="gemini-2.5-flash", min_chars=10, binary="/bin/true")

                def summarize(self, cleaned_turn: str, *, extra_style: str = "") -> DigestSections | None:
                    del cleaned_turn
                    seen_styles.append(extra_style)
                    return DigestSections(done=("**Sweep:** All green.",), checks=("8/8 services",))

            default_tg = FakeTelegram()
            ops_tg = FakeTelegram()
            nightly_tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=default_tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
                summarizer=StyleSpySummarizer(),
                async_summaries=False,
                agent_registry=bridge_cfg.agent_registry,
                rule_engine=bridge_cfg.rule_engine,
                bot_channels={
                    "ops": (ops_tg, "777"),  # type: ignore[dict-item]
                    "nightly": (nightly_tg, "777"),  # type: ignore[dict-item]
                },
            )
            try:
                daemon.poll_herdr_once()
                # Transition all 3 panes to idle simultaneously
                for pid in ("w1:p1", "w2:p1", "w3:p1"):
                    prev = herdr.snapshots[pid]
                    herdr.snapshots[pid] = dataclasses.replace(prev, status="idle", state_change_seq=2)
                daemon.poll_herdr_once()

                # ops and nightly have idle_delay_seconds=0 -> delivered immediately; FeatureWork ("the rest") waits 90s!
                self.assertEqual(len(ops_tg.sent), 1)
                self.assertTrue(ops_tg.sent[0]["disable_notification"])
                self.assertIn("Oncall (🤖 Pro)", ops_tg.sent[0]["text"])
                self.assertIn("Group checks per cluster.", seen_styles)

                self.assertEqual(len(nightly_tg.sent), 1)
                self.assertFalse(nightly_tg.sent[0]["disable_notification"])
                self.assertIn("SecurityRepo (🤖 Opus)", nightly_tg.sent[0]["text"])

                self.assertEqual(len(default_tg.sent), 0)
                clock.advance(91.0)
                daemon.poll_herdr_once()
                self.assertEqual(len(default_tg.sent), 1)
                # Channel handle isolation: w3:p1 on default is also 🤖 Pro without #2!
                self.assertIn("FeatureWork (🤖 Pro)", default_tg.sent[0]["text"])

                # telegram-send --bot ops routes explicitly to the ops bot token
                tg_send_path = Path(__file__).resolve().parent / "telegram-send"
                loader = importlib.machinery.SourceFileLoader("telegram_send_mod_test", str(tg_send_path))
                spec = importlib.util.spec_from_loader(loader.name, loader)
                assert spec is not None
                tg_send_mod = importlib.util.module_from_spec(spec)
                loader.exec_module(tg_send_mod)

                sent_tokens: list[str] = []

                class DummySendClient:
                    def __init__(self, token: str) -> None:
                        sent_tokens.append(token)

                    def send_message(self, chat_id: str, text: str, **_: Any) -> int:
                        del chat_id, text
                        return 501

                rc = tg_send_mod.run_cli(
                    ["--pane", "w3:p1", "--bot", "ops", "Manual ops alert"],
                    env_path=env_path,
                    config_path=toml_path,
                    herdr=herdr,
                    client_factory=DummySendClient,
                    state_dir=Path(tmp) / "cli_state",
                )
                self.assertEqual(rc, 0)
                self.assertEqual(sent_tokens, ["222:ops-tok#literal"])

                # Auto-routing in telegram-send infers custom opus-audit alias and sends via nightly bot!
                rc_auto = tg_send_mod.run_cli(
                    ["--pane", "w2:p1", "Nightly audit summary"],
                    env_path=env_path,
                    config_path=toml_path,
                    herdr=herdr,
                    client_factory=DummySendClient,
                    state_dir=Path(tmp) / "cli_state",
                )
                self.assertEqual(rc_auto, 0)
                self.assertEqual(sent_tokens[-1], "333:nightly-tok")
            finally:
                daemon.close()

    def test_pluggable_agents_builtin_22_kinds_and_custom_profiles(self) -> None:
        self.assertEqual(len(HERDR_BUILTIN_AGENT_KINDS), 22)
        for expected_kind in ("claude", "codex", "gemini", "agy", "opencode", "cursor", "pi", "amp", "droid"):
            self.assertIn(expected_kind, HERDR_BUILTIN_AGENT_KINDS)

        registry = AgentRegistry(
            (
                AgentSpec(
                    name="opus",
                    kind="claude",
                    spawn_args=("--model", "opus"),
                    default_model="opus",
                ),
                AgentSpec(
                    name="aider",
                    kind="aider",
                    model_patterns=(r"(?i)(deepseek-r1)",),
                    prompt_prefixes=("aider>",),
                    footer_patterns=(r"(?i)^Cost:\s+\$[0-9.]+",),
                    default_model="aider",
                ),
            )
        )
        # Custom profile model + turn extraction
        self.assertEqual(
            detect_pane_model("Cost: $0.04 | deepseek-r1", agent_kind="aider", agent_registry=registry),
            "deepseek-r1",
        )
        aider_turn = extract_last_agent_turn(
            "aider> fix login bug\nApplied edit to auth.py and ran pytest.\nCost: $0.04\n",
            agent_kind="aider",
            agent_registry=registry,
        )
        self.assertEqual(aider_turn, "Applied edit to auth.py and ran pytest.")

        # Spawning both a built-in Herdr kind (`gemini`) and a custom profile (`opus` -> `claude --model opus`)
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "demo-repo"
            repo_dir.mkdir()
            herdr = FakeHerdr({})
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                spawn_roots=[Path(tmp)],
                agent_registry=registry,
                async_summaries=False,
            )
            try:
                now_ts = int(time.time())
                daemon.process_updates(
                    [
                        {
                            "update_id": 10,
                            "message": {
                                "message_id": 1,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "text": "/spawn --agent gemini demo-repo",
                            },
                        },
                        {
                            "update_id": 11,
                            "message": {
                                "message_id": 2,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "text": "/spawn -a opus demo-repo",
                            },
                        },
                    ],
                    {},
                )
                self.assertEqual(len(herdr.started_agents), 2)
                self.assertEqual(herdr.started_agents[0][1], "gemini")
                self.assertEqual(herdr.started_agents[1][1], "claude")
                self.assertEqual(herdr.started_extra_args["w23:p1"], ("--model", "opus"))
            finally:
                daemon.close()

    def test_unpaired_start_command_replies_in_private_chat_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = FakeHerdr({})
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="",
                state_path=Path(tmp) / "state.json",
                async_summaries=False,
            )
            try:
                now_ts = int(time.time())
                daemon.process_updates(
                    [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 10,
                                "date": now_ts,
                                "from": {"id": 888},
                                "chat": {"id": -100999, "type": "supergroup"},
                                "text": "/start",
                            },
                        },
                        {
                            "update_id": 2,
                            "message": {
                                "message_id": 11,
                                "date": now_ts,
                                "from": {"id": 888},
                                "chat": {"id": 888, "type": "private"},
                                "text": "run something",
                            },
                        },
                        {
                            "update_id": 3,
                            "message": {
                                "message_id": 12,
                                "date": now_ts,
                                "from": {"id": 888},
                                "chat": {"id": 888, "type": "private"},
                                "text": "/start",
                            },
                        },
                    ],
                    {},
                )
                self.assertEqual(len(herdr.prompts), 0)
                self.assertEqual(len(tg.sent), 1)
                self.assertEqual(tg.sent[0]["chat_id"], "888")
                self.assertIn("TELEGRAM_CHAT_ID=888", tg.sent[0]["text"])
            finally:
                daemon.close()

    def test_inbound_photo_and_document_save_inside_workspace_and_exclude_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_dir = Path(tmp) / "api-repo"
            (ws_dir / ".git").mkdir(parents=True)
            fallback_dir = Path(tmp) / "fallback_state"

            # Verify real git status stays completely clean in BOTH a nested subfolder AND a git worktree!
            git_repo = Path(tmp) / "real-repo"
            git_repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=git_repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=git_repo, check=True)
            (git_repo / "README.md").write_text("hi\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=git_repo, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=git_repo, check=True)

            deep_sub = git_repo / "a" / "b" / "c" / "d" / "e"
            deep_sub.mkdir(parents=True)
            save_inbound_image(make_png_bytes(), workspace_cwd=deep_sub, fallback_state_dir=fallback_dir)
            sub_status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=git_repo, check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(sub_status, "")

            wt_dir = Path(tmp) / "real-wt"
            subprocess.run(["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt_dir)], cwd=git_repo, check=True)
            save_inbound_image(make_png_bytes(), workspace_cwd=wt_dir, fallback_state_dir=fallback_dir)
            wt_status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=wt_dir, check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(wt_status, "")
            self.assertIn(
                ".herdr-telegram/",
                (git_repo / ".git" / "info" / "exclude").read_text(encoding="utf-8"),
            )

            # Verify symlinked .herdr-telegram in untrusted repo is refused and falls back to state dir!
            trap_target = Path(tmp) / "trap-folder"
            trap_target.mkdir()
            evil_repo = Path(tmp) / "evil-repo"
            evil_repo.mkdir()
            (evil_repo / ".herdr-telegram").symlink_to(trap_target)
            redirected = save_inbound_image(make_png_bytes(), workspace_cwd=evil_repo, fallback_state_dir=fallback_dir)
            self.assertTrue(redirected.is_relative_to(fallback_dir))
            self.assertEqual(list(trap_target.iterdir()), [])

            herdr = FakeHerdr({"w15:p1": make_snap("w15:p1", cwd=str(ws_dir))})
            herdr.footer_outputs["w15:p1"] = "🤖 Claude Opus 4.6"
            tg = FakeTelegram()
            tg.files["file_photo_1"] = make_png_bytes(320, 240)
            tg.files["file_doc_1"] = make_png_bytes(800, 600)
            tg.files["file_reject_1"] = make_png_bytes(64, 64)
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state" / "state.json",
                async_summaries=False,
            )
            try:
                snapshots = daemon.poll_herdr_once()
                now_ts = int(time.time())
                # 1. Standard photo with model prefix caption
                daemon.process_updates(
                    [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 10,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "caption": "opus: fix the button alignment",
                                "photo": [
                                    {"file_id": "thumb_ignore", "file_size": 1200, "width": 90, "height": 90},
                                    {"file_id": "file_photo_1", "file_size": 4200, "width": 320, "height": 240},
                                ],
                            },
                        }
                    ],
                    snapshots,
                )
                self.assertEqual(tg.download_calls, ["file_photo_1"])
                self.assertEqual(len(herdr.prompts), 1)
                target_pane, sent_prompt = herdr.prompts[0]
                self.assertEqual(target_pane, "w15:p1")
                self.assertIn("fix the button alignment", sent_prompt)
                media_dir = ws_dir / ".herdr-telegram" / "media"
                saved_files = sorted(media_dir.glob("tg_*.png"))
                self.assertEqual(len(saved_files), 1)
                self.assertIn(str(saved_files[0]), sent_prompt)
                self.assertEqual(stat.S_IMODE(saved_files[0].stat().st_mode), 0o600)
                self.assertIn(
                    ".herdr-telegram/",
                    (ws_dir / ".git" / "info" / "exclude").read_text(encoding="utf-8"),
                )
                self.assertIn("📷 Sent to", tg.sent[-1]["text"])

                # 2. Uncompressed PNG document with bare "opus:" caption
                daemon.process_updates(
                    [
                        {
                            "update_id": 2,
                            "message": {
                                "message_id": 11,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "caption": "opus:",
                                "document": {
                                    "file_id": "file_doc_1",
                                    "file_name": "terminal.png",
                                    "mime_type": "image/png",
                                    "file_size": 5000,
                                },
                            },
                        }
                    ],
                    snapshots,
                )
                self.assertEqual(len(herdr.prompts), 2)
                self.assertIn("Please inspect the attached Telegram image saved locally at", herdr.prompts[1][1])
                self.assertEqual(len(list(media_dir.glob("tg_*.png"))), 2)

                # 3. Rejected prompt cleans up the newly saved image so nothing orphaned remains
                herdr.prompt_ok = False
                herdr.prompt_err = "agent_busy: agent is currently working"
                daemon.process_updates(
                    [
                        {
                            "update_id": 3,
                            "message": {
                                "message_id": 12,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "photo": [{"file_id": "file_reject_1", "file_size": 900}],
                            },
                        }
                    ],
                    snapshots,
                )
                self.assertIn("Could not prompt", tg.sent[-1]["text"])
                self.assertEqual(len(list(media_dir.glob("tg_*.png"))), 2)

                # 4. Media directory pruning enforces max_files cap
                for idx in range(5):
                    p = media_dir / f"tg_20260101_00000{idx}_deadbeef.png"
                    p.write_bytes(make_png_bytes())
                prune_media_dir(media_dir, max_files=3)
                self.assertEqual(len(list(media_dir.glob("tg_*.png"))), 3)
            finally:
                daemon.close()

    def test_inbound_photo_album_coalesces_across_polls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_dir = Path(tmp) / "ui-repo"
            ws_dir.mkdir()
            clock = FakeClock(1000.0)
            herdr = FakeHerdr({"w15:p1": make_snap("w15:p1", cwd=str(ws_dir))})
            tg = FakeTelegram()
            tg.files["alb_1"] = make_png_bytes(100, 100)
            tg.files["alb_2"] = make_png_bytes(120, 100)
            tg.files["alb_3"] = make_gif_bytes(80, 80)
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                clock=clock,
                async_summaries=False,
            )
            try:
                snapshots = daemon.poll_herdr_once()
                now_ts = int(time.time())
                # Poll 1: first two photos arrive at t=1000.0 (caption on 2nd item)
                daemon.process_updates(
                    [
                        {
                            "update_id": 10,
                            "message": {
                                "message_id": 50,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "media_group_id": "grp_99",
                                "photo": [{"file_id": "alb_1", "file_size": 1000}],
                            },
                        },
                        {
                            "update_id": 11,
                            "message": {
                                "message_id": 51,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "media_group_id": "grp_99",
                                "caption": "Compare mobile before and after",
                                "photo": [{"file_id": "alb_2", "file_size": 1000}],
                            },
                        },
                    ],
                    snapshots,
                )
                self.assertEqual(len(herdr.prompts), 0)

                # Poll 2 arrives 1.0s later with 3rd image; quiet timer resets to t=1001.0
                clock.advance(1.0)
                daemon.poll_herdr_once()
                self.assertEqual(len(herdr.prompts), 0)
                daemon.process_updates(
                    [
                        {
                            "update_id": 12,
                            "message": {
                                "message_id": 52,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "media_group_id": "grp_99",
                                "document": {
                                    "file_id": "alb_3",
                                    "file_name": "anim.gif",
                                    "mime_type": "image/gif",
                                    "file_size": 800,
                                },
                            },
                        }
                    ],
                    snapshots,
                )
                # 1.5s after 3rd image: still buffered
                clock.advance(1.5)
                daemon.poll_herdr_once()
                self.assertEqual(len(herdr.prompts), 0)

                # 2.1s after 3rd image: flushed as ONE combined prompt with all 3 files!
                clock.advance(0.6)
                daemon.poll_herdr_once()
                self.assertEqual(len(herdr.prompts), 1)
                pane, combined = herdr.prompts[0]
                self.assertEqual(pane, "w15:p1")
                self.assertIn("Compare mobile before and after", combined)
                self.assertEqual(tg.download_calls, ["alb_1", "alb_2", "alb_3"])
                media_files = list((ws_dir / ".herdr-telegram" / "media").glob("tg_*"))
                self.assertEqual(len(media_files), 3)
                for mf in media_files:
                    self.assertIn(str(mf), combined)
            finally:
                daemon.close()

    def test_inbound_photo_security_guards_pairing_unauth_heic_and_token_redaction(self) -> None:
        # Verify redact_secrets strips bot<token> embedded inside Telegram file URLs
        leaked = "GET https://api.telegram.org/file/bot123456789:AAH_secret_token_value_1234567890/photos/p1.jpg failed"
        redacted = redact_secrets(leaked)
        self.assertNotIn("AAH_secret_token_value", redacted)
        self.assertIn("[TELEGRAM_TOKEN_REDACTED]", redacted)

        with tempfile.TemporaryDirectory() as tmp:
            # 1. Pairing mode ignores photos without calling download_file or replying
            tg_pair = FakeTelegram()
            tg_pair.files["pair_pic"] = make_png_bytes()
            pair_daemon = BridgeDaemon(
                herdr=FakeHerdr({}),  # type: ignore[arg-type]
                telegram=tg_pair,  # type: ignore[arg-type]
                chat_id="",
                state_path=Path(tmp) / "pair_state.json",
                async_summaries=False,
            )
            try:
                now_ts = int(time.time())
                pair_daemon.process_updates(
                    [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 1,
                                "date": now_ts,
                                "from": {"id": 888},
                                "chat": {"id": 888, "type": "private"},
                                "caption": "/start",
                                "photo": [{"file_id": "pair_pic", "file_size": 500}],
                            },
                        }
                    ],
                    {},
                )
                self.assertEqual(tg_pair.download_calls, [])
                self.assertEqual(tg_pair.sent, [])
            finally:
                pair_daemon.close()

            # 2. Unauthorized user, ambiguous pane target, HEIC hint, and disguised non-image magic bytes
            herdr = FakeHerdr(
                {
                    "w1:p1": make_snap("w1:p1", workspace_label="Alpha", agent="claude"),
                    "w2:p1": make_snap("w2:p1", workspace_label="Beta", agent="codex"),
                }
            )
            tg = FakeTelegram()
            tg.files["ambig_pic"] = make_png_bytes()
            tg.files["disguised_heic"] = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1"
            tg.files["disguised_key"] = b"-----BEGIN OPENSSH PRIVATE KEY-----\nsecret"
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                async_summaries=False,
            )
            try:
                snapshots = daemon.poll_herdr_once()
                now_ts = int(time.time())
                # Unauthorized chat/user -> zero downloads
                daemon.process_updates(
                    [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 10,
                                "date": now_ts,
                                "from": {"id": 999},
                                "chat": {"id": 999, "type": "private"},
                                "photo": [{"file_id": "ambig_pic", "file_size": 500}],
                            },
                        }
                    ],
                    snapshots,
                )
                self.assertEqual(tg.download_calls, [])

                # Ambiguous target -> asks user BEFORE calling download_file
                daemon.process_updates(
                    [
                        {
                            "update_id": 2,
                            "message": {
                                "message_id": 11,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "photo": [{"file_id": "ambig_pic", "file_size": 500}],
                            },
                        }
                    ],
                    snapshots,
                )
                self.assertEqual(tg.download_calls, [])
                self.assertIn("Which agent should receive this photo?", tg.sent[-1]["text"])

                # iPhone .heic document -> helpful HEIC hint without downloading
                daemon.process_updates(
                    [
                        {
                            "update_id": 3,
                            "message": {
                                "message_id": 12,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "document": {
                                    "file_id": "heic_doc",
                                    "file_name": "IMG_4102.HEIC",
                                    "mime_type": "image/heic",
                                    "file_size": 12000,
                                },
                            },
                        }
                    ],
                    snapshots,
                )
                self.assertEqual(tg.download_calls, [])
                self.assertIn("HEIC", tg.sent[-1]["text"])

                # Disguised HEIC & non-image bytes rejected after magic-byte sniff
                daemon.process_updates(
                    [
                        {
                            "update_id": 4,
                            "message": {
                                "message_id": 13,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "caption": "alpha: check",
                                "photo": [{"file_id": "disguised_heic", "file_size": 24}],
                            },
                        },
                        {
                            "update_id": 5,
                            "message": {
                                "message_id": 14,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "caption": "alpha: check",
                                "photo": [{"file_id": "disguised_key", "file_size": 42}],
                            },
                        },
                    ],
                    snapshots,
                )
                self.assertEqual(len(herdr.prompts), 0)
                self.assertIn("HEIC", tg.sent[-2]["text"])
                self.assertIn("Unsupported image format", tg.sent[-1]["text"])
            finally:
                daemon.close()

        # 3. Real TelegramClient.download_file HTTP error never exposes bot token
        secret_tok = "123456789:AAH_super_secret_bot_token_value_999"

        class FailFileHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.dumps({"ok": True, "result": {"file_path": "photos/item.jpg", "file_size": 100}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self.send_response(404)
                self.end_headers()

            def log_message(self, fmt: str, *args: Any) -> None:
                del fmt, args

        srv = HTTPServer(("127.0.0.1", 0), FailFileHandler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            client = TelegramClient(secret_tok)
            client.base_url = f"http://127.0.0.1:{port}/bot{secret_tok}"
            client._file_base_url = f"http://127.0.0.1:{port}/file/bot{secret_tok}"
            with self.assertRaises(RuntimeError) as ctx:
                client.download_file("f_404")
            self.assertNotIn(secret_tok, str(ctx.exception))
            self.assertIn("HTTP 404", str(ctx.exception))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_spawn_with_photo_caption_and_cleanup_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "web-app"
            (repo_dir / ".git").mkdir(parents=True)
            herdr = FakeHerdr({})
            tg = FakeTelegram()
            tg.files["spawn_pic_ok"] = make_png_bytes(400, 300)
            tg.files["spawn_pic_fail"] = make_png_bytes(400, 300)
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=Path(tmp) / "state.json",
                spawn_roots=[Path(tmp)],
                async_summaries=False,
            )
            try:
                now_ts = int(time.time())
                # 1. Invalid repo in /spawn caption fails before downloading image
                daemon.process_updates(
                    [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 1,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "caption": "/spawn missing-repo fix layout",
                                "photo": [{"file_id": "spawn_pic_ok", "file_size": 1200}],
                            },
                        }
                    ],
                    {},
                )
                self.assertEqual(tg.download_calls, [])

                # 2. Valid /spawn caption saves image in repo_dir/.herdr-telegram/media and prompts new agent
                daemon.process_updates(
                    [
                        {
                            "update_id": 2,
                            "message": {
                                "message_id": 2,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "caption": "/spawn -a codex web-app match this mockup",
                                "photo": [{"file_id": "spawn_pic_ok", "file_size": 1200}],
                            },
                        }
                    ],
                    {},
                )
                self.assertEqual(tg.download_calls, ["spawn_pic_ok"])
                self.assertEqual(len(herdr.prompts), 1)
                self.assertEqual(herdr.prompts[0][0], "w23:p1")
                self.assertIn("match this mockup", herdr.prompts[0][1])
                media_dir = repo_dir / ".herdr-telegram" / "media"
                saved_ok = list(media_dir.glob("tg_*.png"))
                self.assertEqual(len(saved_ok), 1)
                self.assertIn(str(saved_ok[0]), herdr.prompts[0][1])

                # 3. Failed agent start cleans up the newly downloaded spawn image
                herdr.start_result = AgentStartResult(outcome="failed", code="agent_start_failed", error="boom")
                daemon.process_updates(
                    [
                        {
                            "update_id": 3,
                            "message": {
                                "message_id": 3,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "caption": "/spawn web-app retry",
                                "photo": [{"file_id": "spawn_pic_fail", "file_size": 1200}],
                            },
                        }
                    ],
                    {},
                )
                self.assertEqual(len(list(media_dir.glob("tg_*.png"))), 1)

                # 4. Blocked agent on startup cleans up image and tells user to resend the photo
                tg.files["spawn_pic_blocked"] = make_png_bytes(400, 300)
                herdr.start_result = AgentStartResult(outcome="blocked", code="agent_not_ready", error="trust dialog")
                herdr.spawn_post_start_status = "blocked"
                daemon.process_updates(
                    [
                        {
                            "update_id": 4,
                            "message": {
                                "message_id": 4,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "caption": "/spawn web-app inspect screenshot",
                                "photo": [{"file_id": "spawn_pic_blocked", "file_size": 1200}],
                            },
                        }
                    ],
                    {},
                )
                self.assertEqual(len(list(media_dir.glob("tg_*.png"))), 1)
                self.assertIn("resend the photo", tg.sent[-1]["text"].lower())
            finally:
                daemon.close()

    def test_outbound_telegram_send_photo_validation_method_choice_and_long_caption_split(self) -> None:
        tg_send_path = Path(__file__).resolve().parent / "telegram-send"
        loader = importlib.machinery.SourceFileLoader("telegram_send_photo_mod", str(tg_send_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        tg_send_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(tg_send_mod)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = tmp_path / "env"
            env_path.write_text("TELEGRAM_BOT_TOKEN=111:test-tok\nTELEGRAM_CHAT_ID=777\n", encoding="utf-8")
            state_dir = tmp_path / "state"

            normal_png = tmp_path / "card.png"
            normal_png.write_bytes(make_png_bytes(400, 300))
            tall_png = tmp_path / "fullpage.png"
            tall_png.write_bytes(make_png_bytes(1200, 9500))
            anim_gif = tmp_path / "demo.gif"
            anim_gif.write_bytes(make_gif_bytes(320, 240))
            secret_file = tmp_path / "id_ed25519"
            secret_file.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n", encoding="utf-8")
            symlink_png = tmp_path / "link.png"
            symlink_png.symlink_to(normal_png)

            # Sniff & method selection checks
            self.assertEqual(sniff_image_bytes(normal_png.read_bytes()).width, 400)  # type: ignore[union-attr]
            self.assertEqual(read_outbound_image(normal_png).telegram_method, "sendPhoto")
            self.assertEqual(read_outbound_image(tall_png).telegram_method, "sendDocument")
            self.assertEqual(read_outbound_image(anim_gif).telegram_method, "sendDocument")
            with self.assertRaises(MediaValidationError):
                read_outbound_image(symlink_png)
            with self.assertRaises(MediaValidationError):
                read_outbound_image(secret_file)
            self.assertIn("HEIC", HEIC_HINT_MESSAGE)

            herdr = FakeHerdr({"w15:p1": make_snap("w15:p1", workspace_label="Checkout")})
            herdr.footer_outputs["w15:p1"] = "🤖 Claude Opus 4.6"
            fake_tg = FakeTelegram()

            # 1. Symlink and non-image rejected by CLI with exit code 1 and zero Telegram calls
            with patch("sys.stderr", new_callable=io.StringIO) as err_buf:
                rc_sym = tg_send_mod.run_cli(
                    ["--pane", "w15:p1", "--photo", str(symlink_png), "leak?"],
                    env_path=env_path,
                    herdr=herdr,
                    client_factory=lambda _tok: fake_tg,
                    state_dir=state_dir,
                )
            self.assertEqual(rc_sym, 1)
            self.assertIn("symlink", err_buf.getvalue().lower())
            self.assertEqual(fake_tg.sent_media, [])

            with patch("sys.stderr", new_callable=io.StringIO):
                rc_key = tg_send_mod.run_cli(
                    ["--pane", "w15:p1", "--photo", str(secret_file)],
                    env_path=env_path,
                    herdr=herdr,
                    client_factory=lambda _tok: fake_tg,
                    state_dir=state_dir,
                )
            self.assertEqual(rc_key, 1)
            self.assertEqual(fake_tg.sent_media, [])

            # 2. Short caption (<= 1024) sends single sendPhoto; tall PNG sends sendDocument
            with patch("sys.stdout", new_callable=io.StringIO):
                rc_short = tg_send_mod.run_cli(
                    ["--pane", "w15:p1", "--photo", str(normal_png), "Mobile checkout —sk-1234567890abcdefghij"],
                    env_path=env_path,
                    herdr=herdr,
                    client_factory=lambda _tok: fake_tg,
                    state_dir=state_dir,
                )
                rc_tall = tg_send_mod.run_cli(
                    ["--pane", "w15:p1", "--photo", str(tall_png)],
                    env_path=env_path,
                    herdr=herdr,
                    client_factory=lambda _tok: fake_tg,
                    state_dir=state_dir,
                )
            self.assertEqual((rc_short, rc_tall), (0, 0))
            self.assertEqual(fake_tg.sent_media[0]["method"], "sendPhoto")
            self.assertIn("📷 <b>Checkout (🤖 Opus)</b>", fake_tg.sent_media[0]["caption"])
            self.assertIn("sk-[REDACTED]", fake_tg.sent_media[0]["caption"])
            self.assertNotIn("—", fake_tg.sent_media[0]["caption"])
            self.assertEqual(fake_tg.sent_media[1]["method"], "sendDocument")

            # 3. Long caption (> 1024 chars) sends header-only photo + reply sendMessage and binds BOTH IDs
            long_text = "Detailed visual diff notes. " * 60
            with patch("sys.stdout", new_callable=io.StringIO):
                rc_long = tg_send_mod.run_cli(
                    ["--pane", "w15:p1", "--photo", str(normal_png), long_text],
                    env_path=env_path,
                    herdr=herdr,
                    client_factory=lambda _tok: fake_tg,
                    state_dir=state_dir,
                )
            self.assertEqual(rc_long, 0)
            photo_msg_id = fake_tg.sent_media[-1]["message_id"]
            followup_msg_id = fake_tg.sent[-1]["message_id"]
            self.assertEqual(fake_tg.sent[-1]["reply_to_message_id"], photo_msg_id)

            # Verify swipe-replying to EITHER the photo OR the follow-up long caption routes back to w15:p1!
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=fake_tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=state_dir / "state.json",
                async_summaries=False,
            )
            try:
                snapshots = daemon.poll_herdr_once()
                now_ts = int(time.time())
                daemon.process_updates(
                    [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 500,
                                "date": now_ts,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "text": "ship the mobile layout",
                                "reply_to_message": {"message_id": followup_msg_id, "text": "irrelevant"},
                            },
                        }
                    ],
                    snapshots,
                )
                self.assertEqual(herdr.prompts[-1], ("w15:p1", "ship the mobile layout"))
            finally:
                daemon.close()

            # 4. Real TelegramClient.send_media multipart HTTP upload check
            received_requests: list[tuple[str, str, bytes]] = []

            class MultipartHandler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length)
                    received_requests.append((self.path, self.headers.get("Content-Type", ""), raw))
                    resp = json.dumps({"ok": True, "result": {"message_id": 909}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)

                def log_message(self, fmt: str, *args: Any) -> None:
                    del fmt, args

            srv = HTTPServer(("127.0.0.1", 0), MultipartHandler)
            port = srv.server_address[1]
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                real_client = TelegramClient("999:tok")
                real_client.base_url = f"http://127.0.0.1:{port}/bot999:tok"
                mid = real_client.send_media("777", read_outbound_image(normal_png), caption="<b>Hi</b>")
                self.assertEqual(mid, 909)
                self.assertEqual(len(received_requests), 1)
                req_path, ctype, raw_body = received_requests[0]
                self.assertTrue(req_path.endswith("/sendPhoto"))
                self.assertIn("multipart/form-data; boundary=", ctype)
                self.assertIn(b'name="photo"; filename="card.png"', raw_body)
                self.assertIn(b"<b>Hi</b>", raw_body)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_post_http_error_scrubs_token_cli_versions_route_include_output_and_install_sh(self) -> None:
        import urllib.error

        import herdr_telegram as ht
        import telegram_send
        import ts_preview

        # 1. CLI --version flags exit cleanly with code 0
        for cli_fn in (ht.main, telegram_send.main, ts_preview.main):
            with patch("sys.stdout", new_callable=io.StringIO) as out_buf, self.assertRaises(SystemExit) as sys_ctx:
                cli_fn(["--version"])
            self.assertEqual(sys_ctx.exception.code, 0)
            self.assertIn(ht.__version__, out_buf.getvalue())

        # 2. TelegramClient._post scrubs bot token from HTTPError.url / .filename and extracts description
        secret_tok = "123456789:AAH_post_secret_token_value_999"

        class PostFailHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.dumps({"ok": False, "description": "Bad Request: chat not found"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                del fmt, args

        srv = HTTPServer(("127.0.0.1", 0), PostFailHandler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            client = TelegramClient(secret_tok)
            client.base_url = f"http://127.0.0.1:{port}/bot{secret_tok}"
            with self.assertRaises(urllib.error.HTTPError) as http_ctx:
                client.send_message("777", "hello")
            self.assertEqual(http_ctx.exception.code, 400)
            self.assertNotIn(secret_tok, http_ctx.exception.url)
            self.assertNotIn(secret_tok, str(http_ctx.exception.filename or ""))
            self.assertIn("chat not found", str(http_ctx.exception))
        finally:
            srv.shutdown()
            srv.server_close()

        # 3. Route-level include_output=False respected when rejected prompt hits blocked pane
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            herdr = FakeHerdr(
                {
                    "w9:p1": make_snap(
                        "w9:p1",
                        workspace_label="SecretOps",
                        status="blocked",
                        state_change_seq=5,
                    )
                }
            )
            herdr.visible_outputs["w9:p1"] = "Do you want to run?\n1. Yes\n2. No\n"
            herdr.prompt_ok = False
            herdr.prompt_err = "agent_blocked: waiting"
            tg = FakeTelegram()
            engine = RuleEngine(
                defaults=RouteDefaults(include_output=True),
                routes=(RouteRule(name="quiet", match_workspaces=("SecretOps",), include_output=False),),
            )
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=tmp_path / "state.json",
                rule_engine=engine,
                async_summaries=False,
            )
            try:
                snapshots = daemon.poll_herdr_once()
                daemon.process_updates(
                    [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 1,
                                "date": int(time.time()),
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "text": "ship it",
                            },
                        }
                    ],
                    snapshots,
                )
                # Generic fallback blocked buttons (⏎ Enter) used because visible menu was not read
                btns = [b["text"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
                self.assertIn("⏎ Enter", btns)
            finally:
                daemon.close()

            # 4. install.sh smoke test into custom path containing spaces
            repo_root = Path(__file__).resolve().parent
            custom_base = tmp_path / "Custom Dir"
            install_dir = custom_base / "share" / "herdr-telegram"
            bin_dir = custom_base / "bin"
            cfg_dir = custom_base / "config"
            sysd_dir = custom_base / "systemd"
            env = {
                **os.environ,
                "HOME": str(custom_base),
                "HERDR_TELEGRAM_OS_OVERRIDE": "Linux",
                "HERDR_TELEGRAM_INSTALL_DIR": str(install_dir),
                "HERDR_TELEGRAM_BIN_DIR": str(bin_dir),
                "HERDR_TELEGRAM_CONFIG_DIR": str(cfg_dir),
                "HERDR_TELEGRAM_SYSTEMD_DIR": str(sysd_dir),
                "HERDR_TELEGRAM_SKIP_SYSTEMCTL": "1",
            }
            proc = subprocess.run(
                ["bash", str(repo_root / "install.sh")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertTrue((bin_dir / "telegram-send").is_symlink())
            self.assertTrue((bin_dir / "ts-preview").is_symlink())
            self.assertEqual((cfg_dir / "env").stat().st_mode & 0o777, 0o600)
            self.assertTrue((cfg_dir / "config.example.toml").is_file())
            unit_path = sysd_dir / "herdr-telegram.service"
            unit_text = unit_path.read_text(encoding="utf-8")
            self.assertIn(f"EnvironmentFile=-{cfg_dir}/env", unit_text)
            self.assertIn(f'Environment="HERDR_TELEGRAM_CONFIG_DIR={cfg_dir}"', unit_text)
            self.assertIn(f'"{install_dir}/herdr_telegram.py"', unit_text)
            systemd_analyze = shutil.which("systemd-analyze")
            if systemd_analyze:
                verify_proc = subprocess.run(
                    [systemd_analyze, "--user", "verify", str(unit_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                combined_verify = (verify_proc.stdout + "\n" + verify_proc.stderr).lower()
                self.assertNotIn("environmentfile= path is not absolute", combined_verify)

    def test_bot_menu_commands_upsert_env_seed_environ_doctor_setup_and_darwin_uninstall(self) -> None:
        # 1. TelegramClient.get_me() + set_my_commands() + BridgeDaemon._sync_bot_commands()
        posted: list[tuple[str, dict[str, Any]]] = []

        class CmdMenuHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                posted.append((self.path, payload))
                if self.path.endswith("/getMe"):
                    resp_obj: dict[str, Any] = {"ok": True, "result": {"id": 42, "username": "herdr_test_bot"}}
                else:
                    resp_obj = {"ok": True, "result": True}
                raw = json.dumps(resp_obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: Any) -> None:
                del fmt, args

        srv = HTTPServer(("127.0.0.1", 0), CmdMenuHandler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            tg1 = TelegramClient("111:aaa")
            tg1.base_url = f"http://127.0.0.1:{port}/bot111:aaa"
            tg2 = TelegramClient("222:bbb")
            tg2.base_url = f"http://127.0.0.1:{port}/bot222:bbb"
            me = tg1.get_me()
            self.assertEqual(me["username"], "herdr_test_bot")

            with tempfile.TemporaryDirectory() as tmp:
                daemon = BridgeDaemon(
                    herdr=FakeHerdr({}),  # type: ignore[arg-type]
                    telegram=tg1,
                    chat_id="777",
                    state_path=Path(tmp) / "state.json",
                    bot_channels={"default": (tg1, "777"), "ops": (tg2, "777")},
                    async_summaries=False,
                )
                try:
                    daemon._sync_bot_commands()
                finally:
                    daemon.close()

            menu_calls = [item for item in posted if item[0].endswith("/setMyCommands")]
            self.assertEqual(len(menu_calls), 2)
            expected_cmds = [{"command": c, "description": d} for c, d in BOT_MENU_COMMANDS]
            self.assertEqual(menu_calls[0][1]["commands"], expected_cmds)
            self.assertEqual(menu_calls[1][1]["commands"], expected_cmds)
        finally:
            srv.shutdown()
            srv.server_close()

        # 2. upsert_env_values & seed_environ_from_env_file + HerdrClient child-env scrubbing
        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "111:secret_bot_tok",
                "HERDR_SUMMARY_API_KEY": "sk-summary-secret",
                "OPENAI_API_KEY": "sk-openai-secret",
                "HERDR_BIN": "/usr/bin/herdr",
            },
        ):
            child_env = HerdrClient._build_child_env()
            self.assertNotIn("TELEGRAM_BOT_TOKEN", child_env)
            self.assertNotIn("HERDR_SUMMARY_API_KEY", child_env)
            self.assertNotIn("OPENAI_API_KEY", child_env)
            self.assertEqual(child_env["HERDR_BIN"], "/usr/bin/herdr")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_file = tmp_path / "cfg" / "env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            # Include a duplicate TELEGRAM_CHAT_ID line to verify deduplication on update
            env_file.write_text(
                "# Keep comment\nexport TELEGRAM_BOT_TOKEN=old_tok\nTELEGRAM_CHAT_ID=\nCUSTOM_NOTE=plain\nTELEGRAM_CHAT_ID=stale_111\n",
                encoding="utf-8",
            )
            os.chmod(env_file, 0o644)
            upsert_env_values(
                env_file,
                {
                    "TELEGRAM_BOT_TOKEN": "999:new_tok",
                    "TELEGRAM_CHAT_ID": "12345678",
                    "CUSTOM_NOTE": 'value with # and "quotes"',
                },
            )
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            raw_env = env_file.read_text(encoding="utf-8")
            self.assertIn("# Keep comment", raw_env)
            self.assertIn("export TELEGRAM_BOT_TOKEN=999:new_tok", raw_env)
            self.assertNotIn("stale_111", raw_env)
            parsed = parse_env_file(env_file)
            self.assertEqual(parsed["TELEGRAM_BOT_TOKEN"], "999:new_tok")
            self.assertEqual(parsed["TELEGRAM_CHAT_ID"], "12345678")
            self.assertEqual(parsed["CUSTOM_NOTE"], 'value with # and "quotes"')

            target_env: dict[str, str] = {"TELEGRAM_CHAT_ID": "override_id"}
            seed_environ_from_env_file(env_file, target_environ=target_env)
            self.assertEqual(target_env["TELEGRAM_BOT_TOKEN"], "999:new_tok")
            self.assertEqual(target_env["TELEGRAM_CHAT_ID"], "override_id")

            with self.assertRaises(ValueError):
                upsert_env_values(env_file, {"TELEGRAM_CHAT_ID": "bad\nline"})
            with self.assertRaises(ValueError):
                upsert_env_values(env_file, {"bad-key": "123"})
            sym_env = tmp_path / "sym_env"
            sym_env.symlink_to(env_file)
            with self.assertRaises(ValueError):
                upsert_env_values(sym_env, {"TELEGRAM_CHAT_ID": "1"})

            # 3. run_doctor (--json, --offline, permission warnings, masked chat_id, missing token error)
            spawn_dir = tmp_path / "repos"
            spawn_dir.mkdir()
            upsert_env_values(env_file, {"HERDR_SPAWN_ROOTS": str(spawn_dir), "HERDR_SUMMARY_PROVIDER": "off"})

            class FakeDoctorTelegram:
                def __init__(self, token: str) -> None:
                    self.token = token

                def get_me(self) -> dict[str, Any]:
                    return {"id": 55, "username": "doctor_ok_bot"}

            with patch("sys.stdout", new_callable=io.StringIO) as doc_out:
                rc = run_doctor(
                    as_json=True,
                    env_path=env_file,
                    herdr_factory=lambda: FakeHerdr({"w1:p1": make_snap("w1:p1")}),
                    telegram_factory=FakeDoctorTelegram,
                    service_status_fn=lambda: ("ok", "systemd active"),
                )
            self.assertEqual(rc, 0)
            doc_payload = json.loads(doc_out.getvalue())
            self.assertTrue(doc_payload["ok"])
            bot_check = next(c for c in doc_payload["checks"] if c["name"] == "bot:default")
            self.assertEqual(bot_check["status"], "ok")
            self.assertIn("@doctor_ok_bot", bot_check["detail"])
            self.assertIn("...5678", bot_check["detail"])
            self.assertNotIn("12345678", bot_check["detail"])

            # Loose 0644 permissions trigger warn, missing token triggers error (rc=1)
            empty_env = tmp_path / "empty_env"
            empty_env.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
            os.chmod(empty_env, 0o644)
            with patch("sys.stdout", new_callable=io.StringIO) as bad_out:
                rc_bad = run_doctor(
                    as_json=True,
                    offline=True,
                    env_path=empty_env,
                    herdr_factory=lambda: FakeHerdr({}),
                    service_status_fn=lambda: ("warn", "inactive"),
                )
            self.assertEqual(rc_bad, 1)
            bad_payload = json.loads(bad_out.getvalue())
            self.assertFalse(bad_payload["ok"])
            perm_check = next(c for c in bad_payload["checks"] if c["name"] == "env_permissions")
            self.assertEqual(perm_check["status"], "warn")

            # 4. run_setup interactive pairing flow + non-TTY guard
            with (
                patch("sys.stdin.isatty", return_value=False),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                self.assertEqual(run_setup(env_path=tmp_path / "nty_env"), 2)

            setup_env = tmp_path / "setup_cfg" / "env"
            setup_state_dir = tmp_path / "setup_state"
            svc_events: list[str] = []

            class FakeSetupTelegram:
                sent_messages: list[tuple[str, str]] = []
                commands_set = False

                def __init__(self, token: str) -> None:
                    self.token = token
                    self.poll_count = 0

                def get_me(self) -> dict[str, Any]:
                    if self.token != "777:valid_setup_tok":
                        raise RuntimeError("Unauthorized")
                    return {"id": 77, "username": "pair_me_bot"}

                def set_my_commands(self, commands: Any = None) -> None:
                    del commands
                    FakeSetupTelegram.commands_set = True

                def get_updates(self, offset: int, timeout: int = 2) -> list[dict[str, Any]]:
                    del timeout
                    self.poll_count += 1
                    if offset == -1:
                        return [{"update_id": 500, "message": {"text": "old"}}]
                    return [
                        {
                            "update_id": 501,
                            "message": {"chat": {"id": -999, "type": "group"}, "text": "/start pair_secret_abc"},
                        },
                        {
                            # Unauthenticated stranger sending bare /start without the one-time pair_code is ignored!
                            "update_id": 502,
                            "message": {"chat": {"id": 9999999, "type": "private"}, "text": "/start"},
                        },
                        {
                            "update_id": 503,
                            "message": {
                                "chat": {"id": 8675309, "type": "private"},
                                "text": "/start@pair_me_bot pair_secret_abc",
                            },
                        },
                    ]

                def send_message(self, chat_id: str, text: str, **_: Any) -> int:
                    FakeSetupTelegram.sent_messages.append((chat_id, text))
                    return 1

            with patch("sys.stdout", new_callable=io.StringIO) as setup_out:
                rc_setup = run_setup(
                    env_path=setup_env,
                    state_dir=setup_state_dir,
                    timeout_seconds=10.0,
                    input_fn=lambda _: "y",
                    secret_input_fn=lambda _: "777:valid_setup_tok",
                    telegram_factory=FakeSetupTelegram,
                    service_status_fn=lambda: ("warn", "inactive"),
                    stop_service_fn=lambda: svc_events.append("stop"),
                    start_service_fn=lambda: (svc_events.append("start") or True, "service started"),
                    pair_code_factory=lambda: "pair_secret_abc",
                    require_tty=False,
                )
            self.assertEqual(rc_setup, 0)
            self.assertIn("https://t.me/pair_me_bot?start=pair_secret_abc", setup_out.getvalue())
            self.assertEqual(svc_events, ["stop", "start"])
            self.assertTrue(FakeSetupTelegram.commands_set)
            self.assertEqual(stat.S_IMODE(setup_env.stat().st_mode), 0o600)
            setup_parsed = parse_env_file(setup_env)
            self.assertEqual(setup_parsed["TELEGRAM_BOT_TOKEN"], "777:valid_setup_tok")
            self.assertEqual(setup_parsed["TELEGRAM_CHAT_ID"], "8675309")
            saved_state = BridgeState.load(setup_state_dir / "state.json")
            self.assertEqual(saved_state.get_last_update_id("default"), 503)
            self.assertEqual(FakeSetupTelegram.sent_messages[-1][0], "8675309")

            # If a previously active service was stopped and polling hits 409 Conflict, service is restored and 409 hint shown
            restore_events: list[str] = []

            class ConflictSetupTelegram(FakeSetupTelegram):
                def get_updates(self, offset: int, timeout: int = 2) -> list[dict[str, Any]]:
                    del timeout
                    if offset == -1:
                        return []
                    raise RuntimeError("HTTP Error 409: Conflict")

            with (
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO) as conflict_err,
            ):
                rc_conflict = run_setup(
                    env_path=tmp_path / "conflict_cfg" / "env",
                    state_dir=setup_state_dir,
                    timeout_seconds=5.0,
                    input_fn=lambda _: "y",
                    secret_input_fn=lambda _: "777:valid_setup_tok",
                    telegram_factory=ConflictSetupTelegram,
                    service_status_fn=lambda: ("ok", "systemd active"),
                    stop_service_fn=lambda: restore_events.append("stop"),
                    start_service_fn=lambda: (restore_events.append("restore") or True, "restored"),
                    pair_code_factory=lambda: "code2",
                    require_tty=False,
                )
            self.assertEqual(rc_conflict, 1)
            self.assertEqual(restore_events, ["stop", "restore"])
            self.assertIn("another instance is polling this bot", conflict_err.getvalue())

            # 5. install.sh on simulated Darwin (launchd plist) + --uninstall + --uninstall --purge
            repo_root = Path(__file__).resolve().parent
            mac_home = tmp_path / "Mac Home"
            mac_install = mac_home / "share" / "herdr-telegram"
            mac_bin = mac_home / "bin"
            mac_cfg = mac_home / "config" / "herdr-telegram"
            mac_state = mac_home / "state" / "herdr-telegram"
            mac_launchd = mac_home / "Library" / "LaunchAgents"
            mac_agents_skills = mac_home / ".agents" / "skills"
            mac_claude_skills = mac_home / ".claude" / "skills"
            self.assertTrue((repo_root / "SECURITY.md").is_file())
            skill_doc = (repo_root / "skills" / "herdr-telegram" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("name: herdr-telegram", skill_doc)
            self.assertIn("telegram-send", skill_doc)
            self.assertIn("ts-preview", skill_doc)
            mac_env = {
                **os.environ,
                "HOME": str(mac_home),
                "HERDR_TELEGRAM_OS_OVERRIDE": "Darwin",
                "HERDR_TELEGRAM_SKIP_LAUNCHCTL": "1",
                "HERDR_TELEGRAM_INSTALL_DIR": str(mac_install),
                "HERDR_TELEGRAM_BIN_DIR": str(mac_bin),
                "HERDR_TELEGRAM_CONFIG_DIR": str(mac_cfg),
                "HERDR_TELEGRAM_STATE_DIR": str(mac_state),
                "HERDR_TELEGRAM_LAUNCHD_DIR": str(mac_launchd),
                "HERDR_TELEGRAM_AGENTS_SKILL_DIR": str(mac_agents_skills),
                "HERDR_TELEGRAM_CLAUDE_SKILL_DIR": str(mac_claude_skills),
            }
            inst_proc = subprocess.run(
                ["bash", str(repo_root / "install.sh"), "--install-skill"],
                env=mac_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(inst_proc.returncode, 0, msg=inst_proc.stderr)
            for cli_name in ("herdr-telegram", "telegram-send", "ts-preview"):
                self.assertTrue((mac_bin / cli_name).is_symlink())
            self.assertTrue((mac_agents_skills / "herdr-telegram").is_symlink())
            self.assertTrue((mac_claude_skills / "herdr-telegram" / "SKILL.md").is_file())
            plist_file = mac_launchd / "io.github.jzhao234.herdr-telegram.plist"
            self.assertTrue(plist_file.is_file())
            plist_data = plistlib.loads(plist_file.read_bytes())
            self.assertEqual(plist_data["Label"], "io.github.jzhao234.herdr-telegram")
            self.assertEqual(plist_data["EnvironmentVariables"]["HERDR_TELEGRAM_CONFIG_DIR"], str(mac_cfg))
            self.assertEqual(plist_data["EnvironmentVariables"]["HERDR_TELEGRAM_STATE_DIR"], str(mac_state))
            self.assertEqual(plist_data["StandardOutPath"], f"{mac_state}/bridge.log")

            # Bare --purge without --uninstall is rejected
            bad_purge = subprocess.run(
                ["bash", str(repo_root / "install.sh"), "--purge"],
                env=mac_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertNotEqual(bad_purge.returncode, 0)

            # Replace telegram-send with an external symlink (simulating pipx) to verify --uninstall leaves it untouched!
            external_bin = tmp_path / "external_tg_send"
            external_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            (mac_bin / "telegram-send").unlink()
            (mac_bin / "telegram-send").symlink_to(external_bin)

            # --uninstall removes bridge symlinks/plist/install dir while preserving mac_cfg and external symlinks
            uninst_proc = subprocess.run(
                ["bash", str(repo_root / "install.sh"), "--uninstall"],
                env=mac_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(uninst_proc.returncode, 0, msg=uninst_proc.stderr)
            self.assertFalse(plist_file.exists())
            self.assertFalse((mac_bin / "herdr-telegram").exists())
            self.assertFalse((mac_agents_skills / "herdr-telegram").exists())
            self.assertFalse((mac_claude_skills / "herdr-telegram").exists())
            self.assertTrue((mac_bin / "telegram-send").is_symlink())
            self.assertFalse(mac_install.exists())
            self.assertTrue((mac_cfg / "env").is_file())

            # --uninstall --purge removes bridge files cleanly AND never deletes unrelated files if placed in a shared dir!
            shared_marker = mac_cfg / "other-tool.conf"
            shared_marker.write_text("keep=1\n", encoding="utf-8")
            purge_proc = subprocess.run(
                ["bash", str(repo_root / "install.sh"), "--uninstall", "--purge"],
                env=mac_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(purge_proc.returncode, 0, msg=purge_proc.stderr)
            self.assertFalse((mac_cfg / "env").exists())
            self.assertFalse((mac_cfg / "config.example.toml").exists())
            self.assertTrue(shared_marker.is_file())
            self.assertFalse(mac_state.exists())

    def test_interactive_slash_command_pickers_and_dynamic_chat_autocomplete(self) -> None:
        self.assertEqual(command_slug("My-Claude 2!"), "my_claude_2")
        self.assertEqual(build_dynamic_bot_commands({}), BOT_MENU_COMMANDS)
        self.assertEqual(extract_spawn_picker_agent(""), "claude")
        self.assertEqual(extract_spawn_picker_agent("--agent codex"), "codex")
        self.assertEqual(extract_spawn_picker_agent("-a gemini"), "gemini")
        self.assertIsNone(extract_spawn_picker_agent("demo-repo"))
        self.assertIsNone(extract_spawn_picker_agent("--agent bogus"))

        # 1. HTTP-level editMessageText (including 400 'message is not modified') and scoped setMyCommands
        http_calls: list[tuple[str, dict[str, Any]]] = []

        class EditAndScopeHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                http_calls.append((self.path, payload))
                if self.path.endswith("/editMessageText") and "unchanged" in payload.get("text", ""):
                    err_raw = json.dumps(
                        {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}
                    ).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err_raw)))
                    self.end_headers()
                    self.wfile.write(err_raw)
                    return
                raw = json.dumps({"ok": True, "result": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: Any) -> None:
                del fmt, args

        srv = HTTPServer(("127.0.0.1", 0), EditAndScopeHandler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            real_tg = TelegramClient("999:zzz")
            real_tg.base_url = f"http://127.0.0.1:{port}/bot999:zzz"
            self.assertTrue(real_tg.edit_message_text("777", 42, "<b>Updated</b>"))
            self.assertTrue(real_tg.edit_message_text("777", 42, "unchanged"))
            real_tg.set_my_commands(BOT_MENU_COMMANDS, scope={"type": "chat", "chat_id": 777})
            self.assertEqual(http_calls[-1][1]["scope"], {"type": "chat", "chat_id": 777})
        finally:
            srv.shutdown()
            srv.server_close()

        # 2. End-to-end interactive pickers (/target, /read, /key, /spawn) + space-named /target_<space> & /read_<space>
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root_a = tmp_path / "Personal"
            root_b = tmp_path / "Work"
            (root_a / "herdr-telegram" / ".git").mkdir(parents=True)
            (root_a / "notes-folder").mkdir(parents=True)
            (root_a / "shared-app" / ".git").mkdir(parents=True)
            (root_b / "shared-app" / ".git").mkdir(parents=True)
            (root_a / ".hidden-repo").mkdir(parents=True)
            outside_dir = tmp_path / "outside-secret"
            outside_dir.mkdir(parents=True)
            (root_a / "escape-link").symlink_to(outside_dir)

            discovered = discover_spawn_repos([root_a, root_b])
            specs = [c.repo_spec for c in discovered]
            # Git repos sorted before plain folders; duplicate 'shared-app' disambiguated by root; symlink & dotfile excluded
            self.assertEqual(
                specs,
                ["herdr-telegram", "Personal/shared-app", "Work/shared-app", "notes-folder"],
            )

            # Lead vs review space naming:
            # - Lead agent -> '<space>' ('migration', 'upgrade', '2fa', 'production_support_skill')
            # - Single review agent of a model in a space -> '<space>_<model>' ('upgrade_opus')
            # - 2+ review agents of the same model in a space -> '<space>_<model>_1', '<space>_<model>_2'
            rich_snaps = {
                "w15:p1": make_snap(pane_id="w15:p1", workspace_id="w15", workspace_label="Migration", agent="claude"),
                "w15:p2": make_snap(pane_id="w15:p2", workspace_id="w15", workspace_label="Migration", agent="codex"),
                "w15:p3": make_snap(pane_id="w15:p3", workspace_id="w15", workspace_label="Migration", agent="codex"),
                "w22:p1": make_snap(pane_id="w22:p1", workspace_id="w22", workspace_label="Upgrade", agent="agy"),
                "w22:p5": make_snap(
                    pane_id="w22:p5",
                    workspace_id="w22",
                    workspace_label="Upgrade",
                    agent="claude",
                    name="cl-support-w22-p1",
                ),
                "w25:p1": make_snap(pane_id="w25:p1", workspace_id="w25", workspace_label="2fa", agent="claude"),
                "w2E:p1": make_snap(
                    pane_id="w2E:p1",
                    workspace_id="w2E",
                    workspace_label="~",
                    agent="claude",
                    title="Production support skill",
                ),
            }
            self.assertEqual(
                workspace_lead_and_reviews(rich_snaps)[0],
                ("w15:p1", ["w15:p2", "w15:p3"]),
            )
            # Explicit cowork lead name ('cl-lead-*') on :p2 overrides :p1
            lead_override = {
                "w30:p1": make_snap(
                    pane_id="w30:p1", workspace_id="w30", workspace_label="Auth", name="cx-support-auth"
                ),
                "w30:p2": make_snap(pane_id="w30:p2", workspace_id="w30", workspace_label="Auth", name="cl-lead-auth"),
            }
            self.assertEqual(workspace_lead_and_reviews(lead_override), [("w30:p2", ["w30:p1"])])

            herdr = FakeHerdr(dict(rich_snaps))
            herdr.footer_outputs["w15:p1"] = "  🤖 Opus 5.5 (high)  |  🧠 72%\n"
            herdr.footer_outputs["w15:p2"] = "gpt-5.4 high · Context 2% used\n"
            herdr.footer_outputs["w15:p3"] = "gpt-5.4 high · Context 8% used\n"
            herdr.footer_outputs["w22:p5"] = "  🤖 Opus 5.5 (high)  |  🧠 14%\n"
            tg = FakeTelegram()
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=tmp_path / "state.json",
                spawn_roots=[root_a, root_b],
                async_summaries=False,
            )
            try:
                snapshots = daemon.poll_herdr_once()
                ordered = ordered_space_snapshots(snapshots)
                slugs = assign_space_command_slugs(snapshots)
                self.assertEqual(
                    slugs,
                    {
                        "w15:p1": "migration",
                        "w15:p2": "migration_gpt5_1",
                        "w15:p3": "migration_gpt5_2",
                        "w22:p1": "upgrade",
                        "w22:p5": "upgrade_opus",
                        "w25:p1": "2fa",
                        "w2E:p1": "production_support_skill",
                    },
                )
                self.assertEqual(format_space_label(snapshots["w15:p1"], ordered), "Migration")
                self.assertEqual(format_space_label(snapshots["w15:p2"], ordered), "Migration_gpt5_1")
                self.assertEqual(format_space_label(snapshots["w15:p3"], ordered), "Migration_gpt5_2")
                self.assertEqual(format_space_label(snapshots["w22:p1"], ordered), "Upgrade")
                self.assertEqual(format_space_label(snapshots["w22:p5"], ordered), "Upgrade_opus")
                self.assertEqual(format_space_label(snapshots["w2E:p1"], ordered), "~ · Production support skill")

                # Dynamic per-chat '/' menu uses '<space>' for leads and '<space>_<model>[_#]' for reviews
                self.assertEqual(len(tg.synced_commands), 1)
                synced_cmds, synced_scope = tg.synced_commands[-1]
                self.assertEqual(synced_scope, {"type": "chat", "chat_id": 777})
                cmd_names = [c for c, _ in synced_cmds]
                self.assertIn("target_migration", cmd_names)
                self.assertIn("target_migration_gpt5_1", cmd_names)
                self.assertIn("target_migration_gpt5_2", cmd_names)
                self.assertIn("target_upgrade", cmd_names)
                self.assertIn("target_upgrade_opus", cmd_names)
                self.assertIn("target_2fa", cmd_names)
                self.assertIn("target_production_support_skill", cmd_names)
                self.assertIn("read_upgrade_opus", cmd_names)
                self.assertNotIn("target_opus1", cmd_names)
                self.assertNotIn("target_clear", cmd_names)
                for c_name, c_desc in synced_cmds:
                    self.assertRegex(c_name, r"^[a-z0-9_]{1,32}$")
                    self.assertTrue(1 <= len(c_desc) <= 256)

                # Polling again with unchanged roster does not re-POST setMyCommands
                daemon.poll_herdr_once()
                self.assertEqual(len(tg.synced_commands), 1)

                def send_text(msg_id: int, txt: str) -> None:
                    daemon.handle_update(
                        {
                            "update_id": msg_id,
                            "message": {
                                "message_id": msg_id,
                                "from": {"id": 777},
                                "chat": {"id": 777, "type": "private"},
                                "text": txt,
                            },
                        },
                        snapshots,
                    )

                def tap_button(upd_id: int, msg_id: int, cb_data: str) -> None:
                    self.assertLessEqual(len(cb_data.encode("utf-8")), 64)
                    daemon.handle_update(
                        {
                            "update_id": upd_id,
                            "callback_query": {
                                "id": f"cq_{upd_id}",
                                "from": {"id": 777},
                                "message": {
                                    "message_id": msg_id,
                                    "chat": {"id": 777, "type": "private"},
                                },
                                "data": cb_data,
                            },
                        },
                        snapshots,
                    )

                # Bare /target renders '<Space>' for leads and '<Space>_<model>[_#]' for review panes
                send_text(1, "/target")
                target_card = tg.sent[-1]
                t_buttons = [btn for row in target_card["reply_markup"]["inline_keyboard"] for btn in row]
                self.assertEqual(
                    [b["text"] for b in t_buttons],
                    [
                        "📌 Migration",
                        "📌 Migration_gpt5_1",
                        "📌 Migration_gpt5_2",
                        "📌 Upgrade",
                        "📌 Upgrade_opus",
                        "📌 2fa",
                        "📌 ~ · Production support skill",
                    ],
                )

                # '/target migration' pins the lead pane w15:p1 directly!
                send_text(2, "/target migration")
                self.assertEqual(daemon.state.pinned_target, "w15:p1")

                # Tapping pin:w22:p1 pins Upgrade, syncs target_clear into '/' menu, and edits card in place
                tap_button(3, target_card["message_id"], "pin:w22:p1")
                self.assertEqual(daemon.state.pinned_target, "w22:p1")
                self.assertEqual(tg.callbacks[-1][1], "Pinned Upgrade")
                edited_buttons = [btn for row in tg.edited[-1]["reply_markup"]["inline_keyboard"] for btn in row]
                self.assertIn("pin:clear", [b["callback_data"] for b in edited_buttons])
                self.assertIn("target_clear", [c for c, _ in tg.synced_commands[-1][0]])

                # Dynamic '/target_migration_gpt5_2', '/target_upgrade_opus', '/target_2fa', and prefix routing
                send_text(4, "/target_migration_gpt5_2")
                self.assertEqual(daemon.state.pinned_target, "w15:p3")
                send_text(5, "/target_upgrade_opus")
                self.assertEqual(daemon.state.pinned_target, "w22:p5")
                send_text(6, "/target_2fa")
                self.assertEqual(daemon.state.pinned_target, "w25:p1")
                send_text(7, "/target_production_support_skill")
                self.assertEqual(daemon.state.pinned_target, "w2E:p1")
                send_text(8, "/target_clear")
                self.assertIsNone(daemon.state.pinned_target)
                self.assertIn("Cleared pinned", tg.sent[-1]["text"])
                send_text(9, "migration: ship lead")
                self.assertEqual(herdr.prompts[-1], ("w15:p1", "ship lead"))
                send_text(10, "upgrade_opus: audit diff")
                self.assertEqual(herdr.prompts[-1], ("w22:p5", "audit diff"))

                # Bare /read opens space+review picker; '/read_upgrade_opus' reads w22:p5 directly
                send_text(11, "/read")
                r_buttons = [btn for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for btn in row]
                self.assertIn("📖 Upgrade_opus", [b["text"] for b in r_buttons])
                send_text(12, "/read_upgrade_opus")
                self.assertEqual(herdr.read_calls[-1][0], "w22:p5")

                # Bare /key opens space picker -> kpick:w15:p2 edits into keypad -> kraw:w15:p2:ctrl+c sends key
                send_text(13, "/key")
                key_msg = tg.sent[-1]
                ka_buttons = [btn for row in key_msg["reply_markup"]["inline_keyboard"] for btn in row]
                self.assertIn("⌨️ Migration_gpt5_1", [b["text"] for b in ka_buttons])
                tap_button(14, key_msg["message_id"], "kpick:w15:p2")
                kp_buttons = [btn for row in tg.edited[-1]["reply_markup"]["inline_keyboard"] for btn in row]
                for btn in kp_buttons:
                    self.assertLessEqual(len(btn["callback_data"].encode("utf-8")), 64)
                self.assertIn("kraw:w15:p2:ctrl+c", [b["callback_data"] for b in kp_buttons])
                self.assertIn("kpick:__list__", [b["callback_data"] for b in kp_buttons])
                tap_button(15, key_msg["message_id"], "kraw:w15:p2:ctrl+c")
                self.assertEqual(herdr.keys[-1], ("w15:p2", ["ctrl+c"]))

                # 1-arg '/key upgrade_opus' jumps straight to Upgrade_opus's keypad
                send_text(16, "/key upgrade_opus")
                direct_kp = [
                    btn["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for btn in row
                ]
                self.assertIn("kraw:w22:p5:enter", direct_kp)

                # Bare /spawn opens repo+agent picker -> spag:codex switches agent -> sprp:codex:<tok> spawns once!
                send_text(17, "/spawn")
                sp_msg = tg.sent[-1]
                sp_btns_initial = [btn for row in sp_msg["reply_markup"]["inline_keyboard"] for btn in row]
                self.assertIn("spag:codex", [b["callback_data"] for b in sp_btns_initial])
                tap_button(18, sp_msg["message_id"], "spag:codex")
                sp_btns_codex = [btn for row in tg.edited[-1]["reply_markup"]["inline_keyboard"] for btn in row]
                repo_btn = next(b for b in sp_btns_codex if b["text"].endswith("herdr-telegram"))
                self.assertTrue(repo_btn["callback_data"].startswith("sprp:codex:"))
                tap_button(19, sp_msg["message_id"], repo_btn["callback_data"])
                self.assertEqual(len(herdr.created_workspaces), 1)
                self.assertEqual(herdr.started_agents[-1][1], "codex")
                # Accidental double-tap on the same spawn picker card is ignored cleanly
                tap_button(20, sp_msg["message_id"], repo_btn["callback_data"])
                self.assertEqual(len(herdr.created_workspaces), 1)
                self.assertIn("already started", tg.callbacks[-1][1])
            finally:
                daemon.close()

    def test_voice_note_transcription_spoken_routing_picker_and_backends(self) -> None:
        # 1. Container magic-byte sniffing & Telegram payload bounds
        sample_ogg = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead"
        sample_wav = b"RIFF\x24\x00\x00\x00WAVEfmt "
        sample_m4a = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00"
        sample_heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
        sample_mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00"
        self.assertEqual(sniff_audio_bytes(sample_ogg), (".ogg", "audio/ogg"))
        self.assertEqual(sniff_audio_bytes(sample_wav), (".wav", "audio/wav"))
        self.assertEqual(sniff_audio_bytes(sample_m4a), (".m4a", "audio/mp4"))
        self.assertIsNone(sniff_audio_bytes(sample_heic))
        self.assertEqual(sniff_audio_bytes(sample_mp3), (".mp3", "audio/mpeg"))
        self.assertIsNone(sniff_audio_bytes(b"not-an-audio-file"))

        cand_voice = inspect_inbound_voice_message({"voice": {"file_id": "v1", "file_size": 12000, "duration": 9}})
        self.assertIsNotNone(cand_voice)
        self.assertIn(
            "20 MB",
            str(inspect_inbound_voice_message({"voice": {"file_id": "v_big", "file_size": 25 * 1024 * 1024}})),
        )
        self.assertIn(
            "10-minute",
            str(inspect_inbound_voice_message({"voice": {"file_id": "v_long", "duration": 601}})),
        )
        cand_doc = inspect_inbound_voice_message(
            {"document": {"file_id": "d_aud", "mime_type": "audio/mp4", "file_name": "memo.m4a", "file_size": 4096}}
        )
        self.assertIsNotNone(cand_doc)

        # 2. Backend resolution priority: local voice-venv -> openai -> groq -> gemini-api -> unconfigured
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_py = tmp_path / "voice-venv" / "bin" / "python"
            fake_py.parent.mkdir(parents=True)
            fake_py.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "assert sys.argv[1] == '-c'\n"
                "audio_p, model_n, vocab = sys.argv[3], sys.argv[4], sys.argv[5]\n"
                "assert pathlib.Path(audio_p).is_file()\n"
                "assert 'TELEGRAM_BOT_TOKEN' not in os.environ\n"
                "payload = pathlib.Path(audio_p).read_bytes()\n"
                "spoken = payload.split(b'|TEXT:', 1)[1].decode('utf-8') if b'|TEXT:' in payload else 'hello world'\n"
                "print(json.dumps({'text': spoken, 'model': model_n, 'offline': os.environ.get('HF_HUB_OFFLINE', '')}))\n",
                encoding="utf-8",
            )
            fake_py.chmod(0o755)

            self.assertEqual(
                resolve_voice_backend("auto", env={"HERDR_TELEGRAM_STATE_DIR": str(tmp_path)})[:2],
                ("local", "large-v3-turbo"),
            )
            self.assertEqual(
                resolve_voice_backend("auto", "local:small.en", local_python=str(fake_py), env={})[:2],
                ("local", "small.en"),
            )
            self.assertEqual(
                resolve_voice_backend("auto", local_python="", env={"OPENAI_API_KEY": "sk-test"})[:2],
                ("openai", "gpt-4o-mini-transcribe"),
            )
            self.assertEqual(
                resolve_voice_backend("auto", local_python="", env={"GROQ_API_KEY": "gsk-test"})[:2],
                ("groq", "whisper-large-v3-turbo"),
            )
            self.assertEqual(
                resolve_voice_backend("auto", local_python="", env={"GEMINI_API_KEY": "gem-test"})[:2],
                ("gemini-api", "gemini-2.5-flash"),
            )
            self.assertEqual(resolve_voice_backend("off", local_python=str(fake_py), env={})[0], "off")
            self.assertEqual(resolve_voice_backend("auto", local_python="", env={})[0], "unconfigured")

            # Verify scrubbed local faster-whisper subprocess execution & temp file cleanup
            prev_tok = os.environ.get("TELEGRAM_BOT_TOKEN")
            os.environ["TELEGRAM_BOT_TOKEN"] = "123456:SECRET_SHOULD_NOT_LEAK"
            try:
                local_tr = VoiceTranscriber(provider="local", model="large-v3-turbo", local_python=str(fake_py))
                heard = local_tr.transcribe_bytes(
                    sample_ogg + b"|TEXT:Upgrade, run the auth suite.",
                    workspace_labels=["Upgrade", "Migration"],
                )
                self.assertEqual(heard, "Upgrade, run the auth suite.")
            finally:
                if prev_tok is None:
                    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                else:
                    os.environ["TELEGRAM_BOT_TOKEN"] = prev_tok

            # Verify cloud OpenAI multipart HTTP transcription on loopback
            stt_requests: list[tuple[str, bytes, str]] = []

            class MockSttHandler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    n = int(self.headers.get("Content-Length", "0"))
                    raw_b = self.rfile.read(n)
                    stt_requests.append((self.path, raw_b, str(self.headers.get("Content-Type") or "")))
                    resp_b = json.dumps({"text": "transcribed via http"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp_b)))
                    self.end_headers()
                    self.wfile.write(resp_b)

                def log_message(self, fmt: str, *args: Any) -> None:
                    del fmt, args

            srv = HTTPServer(("127.0.0.1", 0), MockSttHandler)
            port = srv.server_address[1]
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                http_tr = VoiceTranscriber(
                    provider="openai",
                    model="whisper-1",
                    api_key="sk-local",
                    api_url=f"http://127.0.0.1:{port}/v1",
                    local_python="",
                )
                self.assertEqual(
                    http_tr.transcribe_bytes(sample_ogg, workspace_labels=["Upgrade"]),
                    "transcribed via http",
                )
                self.assertTrue(stt_requests[-1][0].endswith("/v1/audio/transcriptions"))
                self.assertIn("multipart/form-data", stt_requests[-1][2])
                self.assertIn(b"Upgrade", stt_requests[-1][1])
            finally:
                srv.shutdown()
                srv.server_close()

            # 3. Spoken prefix guardrails: only exact active space/handle followed by ','/':'/';' strips prefix
            t_ok, b_ok = extract_spoken_space_prefix(
                "Migration gpt5 2, check the diff.",
                allowed_tokens=["migration", "migration_gpt5_1", "migration_gpt5_2", "upgrade"],
                resolve_fn=lambda c: "w15:p3" if c == "migration_gpt5_2" else None,
            )
            self.assertEqual((t_ok, b_ok), ("w15:p3", "check the diff."))
            # Normal sentence starting with a space name WITHOUT pause punctuation is NOT stripped
            t_no_punct, b_no_punct = extract_spoken_space_prefix(
                "Upgrade the dependencies in package.json",
                allowed_tokens=["upgrade"],
                resolve_fn=lambda c: "w22:p1" if c.lower() == "upgrade" else None,
            )
            self.assertIsNone(t_no_punct)
            self.assertEqual(b_no_punct, "Upgrade the dependencies in package.json")
            # Leading word 'First,' not in active spaces is NOT stripped
            t_unknown, _ = extract_spoken_space_prefix(
                "First, check the logs",
                allowed_tokens=["upgrade", "migration"],
                resolve_fn=lambda _: "w22:p1",
            )
            self.assertIsNone(t_unknown)

            # 4. End-to-end BridgeDaemon voice workflow: spoken prefix, swipe-reply, unpinned picker, and /spawn audio
            spawn_root = tmp_path / "Personal"
            (spawn_root / "herdr-telegram" / ".git").mkdir(parents=True)
            herdr = FakeHerdr(
                {
                    "w15:p1": make_snap(
                        pane_id="w15:p1", workspace_id="w15", workspace_label="Migration", agent="claude"
                    ),
                    "w22:p1": make_snap(pane_id="w22:p1", workspace_id="w22", workspace_label="Upgrade", agent="agy"),
                    "w22:p5": make_snap(
                        pane_id="w22:p5",
                        workspace_id="w22",
                        workspace_label="Upgrade",
                        agent="claude",
                        name="cl-support-w22-p1",
                    ),
                }
            )
            herdr.footer_outputs["w22:p5"] = "  🤖 Opus 5.5 (high)  |  🧠 14%\n"
            tg = FakeTelegram()
            tg.files["v_spoken"] = sample_ogg + b"|TEXT:Upgrade opus: review the error handling"
            tg.files["v_swipe"] = sample_ogg + b"|TEXT:looks good, merge it"
            tg.files["v_unpinned"] = sample_ogg + b"|TEXT:check why the staging deploy stalled"
            tg.files["v_spawn"] = sample_m4a + b"|TEXT:audit the CLI flags"

            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=tmp_path / "state.json",
                spawn_roots=[spawn_root],
                async_summaries=False,
                transcriber=VoiceTranscriber(provider="local", local_python=str(fake_py)),
            )
            try:
                snapshots = daemon.poll_herdr_once()

                # (a) Spoken space prefix routes straight to Upgrade_opus (w22:p5) and echoes transcript in <blockquote>
                daemon.handle_update(
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 50,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "voice": {"file_id": "v_spoken", "file_size": 4096, "duration": 4},
                        },
                    },
                    snapshots,
                )
                self.assertEqual(herdr.prompts[-1], ("w22:p5", "review the error handling"))
                self.assertIn("🎙️ Sent to", tg.sent[-1]["text"])
                self.assertIn("<blockquote>review the error handling</blockquote>", tg.sent[-1]["text"])

                # (b) Swipe-reply voice note to an alert routes straight to that bound pane
                daemon.state.record_message(900, "w15:p1")
                daemon.handle_update(
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 51,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "reply_to_message": {"message_id": 900, "text": "✅ Done · Migration (🤖 Claude)"},
                            "voice": {"file_id": "v_swipe", "file_size": 4096, "duration": 3},
                        },
                    },
                    snapshots,
                )
                self.assertEqual(herdr.prompts[-1], ("w15:p1", "looks good, merge it"))

                # (c) Unpinned voice note with no prefix renders one-tap space picker; tapping delivers without re-recording
                daemon.handle_update(
                    {
                        "update_id": 3,
                        "message": {
                            "message_id": 52,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "voice": {"file_id": "v_unpinned", "file_size": 4096, "duration": 5},
                        },
                    },
                    snapshots,
                )
                picker_msg = tg.sent[-1]
                self.assertIn("Transcribed voice note:", picker_msg["text"])
                v_btns = [btn for row in picker_msg["reply_markup"]["inline_keyboard"] for btn in row]
                up_btn = next(b for b in v_btns if b["text"] == "📌 Upgrade")
                self.assertLessEqual(len(up_btn["callback_data"].encode()), 64)
                daemon.handle_update(
                    {
                        "update_id": 4,
                        "callback_query": {
                            "id": "cq_v1",
                            "from": {"id": 777},
                            "message": {"message_id": picker_msg["message_id"], "chat": {"id": 777, "type": "private"}},
                            "data": up_btn["callback_data"],
                        },
                    },
                    snapshots,
                )
                self.assertEqual(herdr.prompts[-1], ("w22:p1", "check why the staging deploy stalled"))
                # Double-tapping the same voice picker button does not re-send
                daemon.handle_update(
                    {
                        "update_id": 5,
                        "callback_query": {
                            "id": "cq_v2",
                            "from": {"id": 777},
                            "message": {"message_id": picker_msg["message_id"], "chat": {"id": 777, "type": "private"}},
                            "data": up_btn["callback_data"],
                        },
                    },
                    snapshots,
                )
                self.assertIn("expired or already sent", tg.callbacks[-1][1])

                # (d) Shared audio file with '/spawn herdr-telegram' caption spawns workspace with transcribed prompt
                daemon.handle_update(
                    {
                        "update_id": 6,
                        "message": {
                            "message_id": 53,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "caption": "/spawn herdr-telegram",
                            "audio": {"file_id": "v_spawn", "file_size": 4096, "duration": 4},
                        },
                    },
                    snapshots,
                )
                self.assertEqual(len(herdr.created_workspaces), 1)
                self.assertEqual(herdr.prompts[-1][1], "audit the CLI flags")

                # (e) Doctor reports voice as 'ok' (optional) when not installed under auto, and 'warn' only if forced
                from bridge.cli import run_doctor

                no_voice_env = tmp_path / "no_voice.env"
                no_voice_env.write_text(
                    f"TELEGRAM_BOT_TOKEN=111:aaa\nTELEGRAM_CHAT_ID=777\nHERDR_TELEGRAM_STATE_DIR={tmp_path / 'empty_state'}\n",
                    encoding="utf-8",
                )
                no_voice_env.chmod(0o600)
                with patch("sys.stdout", new_callable=io.StringIO) as doc_out:
                    rc_auto = run_doctor(
                        as_json=True,
                        offline=True,
                        env_path=no_voice_env,
                        herdr_factory=lambda: herdr,
                        service_status_fn=lambda: ("ok", "active"),
                    )
                self.assertEqual(rc_auto, 0)
                doc_checks = {c["name"]: c for c in json.loads(doc_out.getvalue())["checks"]}
                self.assertEqual(doc_checks["voice"]["status"], "ok")
                self.assertIn("optional", doc_checks["voice"]["detail"])

                forced_env = tmp_path / "forced_voice.env"
                forced_env.write_text(
                    f"TELEGRAM_BOT_TOKEN=111:aaa\nTELEGRAM_CHAT_ID=777\nHERDR_VOICE_PROVIDER=local\nHERDR_TELEGRAM_STATE_DIR={tmp_path / 'empty_state'}\n",
                    encoding="utf-8",
                )
                forced_env.chmod(0o600)
                with patch("sys.stdout", new_callable=io.StringIO) as doc_out2:
                    run_doctor(
                        as_json=True,
                        offline=True,
                        env_path=forced_env,
                        herdr_factory=lambda: herdr,
                        service_status_fn=lambda: ("ok", "active"),
                    )
                doc_checks2 = {c["name"]: c for c in json.loads(doc_out2.getvalue())["checks"]}
                self.assertEqual(doc_checks2["voice"]["status"], "warn")
            finally:
                daemon.close()

    def test_reply_context_injected_for_older_messages_and_manual_quotes_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            herdr = FakeHerdr(
                {
                    "w15:p1": make_snap(pane_id="w15:p1", workspace_id="w15", workspace_label="Auth", agent="claude"),
                    "w22:p1": make_snap(pane_id="w22:p1", workspace_id="w22", workspace_label="Upgrade", agent="agy"),
                }
            )
            fake_py = tmp_path / "fake_whisper.py"
            fake_py.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "payload = pathlib.Path(sys.argv[3]).read_bytes()\n"
                "spoken = payload.split(b'|TEXT:', 1)[1].decode('utf-8')\n"
                "print(json.dumps({'text': spoken}))\n",
                encoding="utf-8",
            )
            fake_py.chmod(0o755)
            tg = FakeTelegram()
            tg.files["v_old_reply"] = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead|TEXT:revert only that flag"
            daemon = BridgeDaemon(
                herdr=herdr,  # type: ignore[arg-type]
                telegram=tg,  # type: ignore[arg-type]
                chat_id="777",
                state_path=tmp_path / "state.json",
                async_summaries=False,
                transcriber=VoiceTranscriber(provider="local", local_python=str(fake_py)),
            )
            try:
                snapshots = daemon.poll_herdr_once()
                # Auth sent msg 100 earlier, then Upgrade sent msg 105 (so 105 is latest in chat, 100 is older)
                daemon.state.record_message(100, "w15:p1")
                daemon.state.record_message(105, "w22:p1")

                # 1. Replying to the MOST RECENT message (105) sends plain reply text without wrapper noise
                daemon.handle_update(
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 201,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "ship it",
                            "reply_to_message": {
                                "message_id": 105,
                                "text": "✅ Done · Upgrade (🤖 Agy)\n• Bumped dependencies.",
                            },
                        },
                    },
                    snapshots,
                )
                self.assertEqual(herdr.prompts[-1], ("w22:p1", "ship it"))

                # 2. Replying to an OLDER message (100 < latest) automatically prepends the replied-to message context
                #    while stripping any trailing 'Swipe-reply or prefix ...' boilerplate footer
                daemon.handle_update(
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 202,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "add a rate limit here too",
                            "reply_to_message": {
                                "message_id": 100,
                                "text": (
                                    "✅ Done · Auth (🤖 Claude)\n"
                                    "• Added email 2FA verification endpoint.\n"
                                    "Swipe-reply or prefix claude: to reply."
                                ),
                            },
                        },
                    },
                    snapshots,
                )
                sent_target, sent_prompt = herdr.prompts[-1]
                self.assertEqual(sent_target, "w15:p1")
                self.assertEqual(
                    sent_prompt,
                    "[Replying to earlier Telegram message]\n"
                    "> ✅ Done · Auth (🤖 Claude)\n"
                    "> • Added email 2FA verification endpoint.\n\n"
                    "add a rate limit here too",
                )

                # 3. Highlighting a partial Telegram quote (is_manual=True) includes the excerpt even on the latest card
                latest_id = max(int(k) for k in daemon.state.get_message_targets("default"))
                daemon.state.record_message(latest_id + 10, "w22:p1")
                newest_id = latest_id + 10
                daemon.handle_update(
                    {
                        "update_id": 3,
                        "message": {
                            "message_id": 203,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "prune journal logs now",
                            "reply_to_message": {
                                "message_id": newest_id,
                                "text": "✅ Done · Upgrade\n🟢 HTTP ok\n🔴 [P1] billing-api: disk at 94%",
                            },
                            "quote": {
                                "text": "🔴 [P1] billing-api: disk at 94%",
                                "is_manual": True,
                            },
                        },
                    },
                    snapshots,
                )
                self.assertEqual(
                    herdr.prompts[-1],
                    (
                        "w22:p1",
                        "[Replying to quoted excerpt on Telegram]\n"
                        "> 🔴 [P1] billing-api: disk at 94%\n\n"
                        "prune journal logs now",
                    ),
                )

                # 4. Replying to a bare 1-line delivery ack ('➡️ Sent to ...') does not prepend boilerplate ack noise
                daemon.handle_update(
                    {
                        "update_id": 4,
                        "message": {
                            "message_id": 204,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "text": "status?",
                            "reply_to_message": {
                                "message_id": 100,
                                "text": "➡️ Sent to Auth (🤖 Claude).",
                            },
                        },
                    },
                    snapshots,
                )
                self.assertEqual(herdr.prompts[-1], ("w15:p1", "status?"))

                # 5. Voice-note swipe-reply to an older card injects context for the agent while keeping echo clean
                daemon.handle_update(
                    {
                        "update_id": 5,
                        "message": {
                            "message_id": 205,
                            "from": {"id": 777},
                            "chat": {"id": 777, "type": "private"},
                            "reply_to_message": {
                                "message_id": 100,
                                "text": "💬 Auth (🤖 Claude)\nEnabled strict TOTP enforcement.",
                            },
                            "voice": {"file_id": "v_old_reply", "file_size": 4096, "duration": 3},
                        },
                    },
                    snapshots,
                )
                self.assertEqual(
                    herdr.prompts[-1],
                    (
                        "w15:p1",
                        "[Replying to earlier Telegram message]\n"
                        "> 💬 Auth (🤖 Claude)\n"
                        "> Enabled strict TOTP enforcement.\n\n"
                        "revert only that flag",
                    ),
                )
                self.assertIn("<blockquote>revert only that flag</blockquote>", tg.sent[-1]["text"])
                self.assertNotIn("[Replying to earlier Telegram message]", tg.sent[-1]["text"])
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
