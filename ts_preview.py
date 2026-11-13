#!/usr/bin/env python3
"""Manage Tailscale Serve mobile preview URLs and optionally push them to Telegram."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from bridge import __version__
except ImportError:
    __version__ = "0.2.0"


def _resolve_default_tailscale_bin() -> str:
    if shutil.which("tailscale"):
        return "tailscale"
    mac_app_bin = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    if mac_app_bin.is_file() and os.access(mac_app_bin, os.X_OK):
        return str(mac_app_bin)
    return "tailscale"


DEFAULT_TAILSCALE_BIN = _resolve_default_tailscale_bin()


def _resolve_default_telegram_send_bin() -> str:
    candidates: list[Path] = [Path(__file__).resolve().parent / "telegram-send"]
    scripts_dir = sysconfig.get_path("scripts")
    if scripts_dir:
        candidates.append(Path(scripts_dir) / "telegram-send")
    if sys.executable:
        candidates.append(Path(sys.executable).resolve().parent / "telegram-send")
    candidates.append(Path.home() / ".local" / "bin" / "telegram-send")
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    found = shutil.which("telegram-send")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "telegram-send")


DEFAULT_TELEGRAM_SEND_BIN = _resolve_default_telegram_send_bin()
DEFAULT_SERVE_TIMEOUT_SECONDS = 8.0
ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_SCHEMES = frozenset({"http", "https", "https+insecure"})
ENABLE_URL_RE = re.compile(r"https://login\.tailscale\.com/[^\s]+")


class PreviewError(RuntimeError):
    """User-facing error raised by ts_preview operations."""


@dataclass(frozen=True)
class TailscaleState:
    hostname: str
    dns_name: str
    ipv4: str
    cert_domains: tuple[str, ...]
    operator_user: str

    @property
    def https_ready(self) -> bool:
        return bool(self.cert_domains)


@dataclass(frozen=True)
class TargetSpec:
    url: str
    local_port: int
    host: str
    scheme: str
    path: str = ""


@dataclass(frozen=True)
class PreviewEntry:
    protocol: str  # "https" or "http"
    external_port: int
    mount_path: str
    target: str
    url: str
    foreground: bool = False


@dataclass(frozen=True)
class ServeResult:
    entry: PreviewEntry
    fell_back_to_http: bool
    enable_https_url: str | None = None
    local_port_listening: bool = True
    telegram_sent: bool = False


class SubprocessRunner:
    """Thin wrapper around subprocess.run for dependency injection in tests."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = DEFAULT_SERVE_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


def parse_target_spec(raw: str) -> TargetSpec:
    """Parse a port or localhost URL into a normalized TargetSpec.

    Refuses non-loopback hosts so ts-preview only exposes local dev servers.
    """
    value = raw.strip()
    if not value:
        raise PreviewError("Target cannot be empty. Pass a port (e.g. 3000) or localhost URL.")

    if value.isdigit():
        port = int(value)
        _validate_port(port)
        return TargetSpec(
            url=f"http://127.0.0.1:{port}",
            local_port=port,
            host="localhost",
            scheme="http",
            path="",
        )

    if "://" not in value:
        value = f"http://{value}"

    parsed = urlparse(value)
    if parsed.username or parsed.password:
        raise PreviewError("Target URL must not include credentials.")
    if parsed.query or parsed.fragment:
        raise PreviewError("Target URL must not include query strings or fragments.")

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_SCHEMES))
        raise PreviewError(f"Unsupported target scheme '{scheme}'. Allowed schemes: {allowed}.")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_LOCAL_HOSTS:
        raise PreviewError(
            f"Refusing non-local target host '{host or value}'. Only localhost / 127.0.0.1 / [::1] targets are allowed."
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise PreviewError(f"Invalid port in target '{raw}'.") from exc

    if port is None:
        raise PreviewError(f"Target '{raw}' must include an explicit port (e.g. localhost:3000).")
    _validate_port(port)

    norm_host = "127.0.0.1" if host in {"localhost", "127.0.0.1"} else "[::1]"
    path = parsed.path or ""
    if path == "/":
        path = ""
    normalized_url = f"{scheme}://{norm_host}:{port}{path}"
    return TargetSpec(url=normalized_url, local_port=port, host=host, scheme=scheme, path=path)


def normalize_mount_path(raw_path: str) -> str:
    """Validate and normalize a --path mount point, preserving trailing slash semantics."""
    cleaned = raw_path.strip()
    if not cleaned:
        return "/"
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    if any(ch.isspace() for ch in cleaned) or "?" in cleaned or "#" in cleaned:
        raise PreviewError(f"Invalid mount path '{raw_path}'. Must be a clean URL path starting with '/'.")
    return cleaned


def _validate_port(port: int) -> None:
    if not (1 <= port <= 65535):
        raise PreviewError(f"Port must be between 1 and 65535 (got {port}).")


def is_local_port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """Return True if a local TCP listener accepts connections on the requested loopback host."""
    if host == "localhost":
        candidates = ["127.0.0.1", "::1"]
    elif host == "::1":
        candidates = ["::1"]
    else:
        candidates = ["127.0.0.1"]

    for candidate in candidates:
        family = socket.AF_INET6 if ":" in candidate else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                if sock.connect_ex((candidate, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def resolve_reachable_target(target: TargetSpec) -> tuple[TargetSpec, bool]:
    """Ensure the proxied loopback address matches the address family actually listening."""
    if target.host == "127.0.0.1":
        return target, is_local_port_listening(target.local_port, "127.0.0.1")
    if target.host == "::1":
        return target, is_local_port_listening(target.local_port, "::1")

    if is_local_port_listening(target.local_port, "127.0.0.1"):
        return target, True
    if is_local_port_listening(target.local_port, "::1"):
        ipv6_url = f"{target.scheme}://[::1]:{target.local_port}{target.path}"
        return (
            TargetSpec(
                url=ipv6_url,
                local_port=target.local_port,
                host="::1",
                scheme=target.scheme,
                path=target.path,
            ),
            True,
        )
    return target, False


def format_preview_url(protocol: str, dns_name: str, external_port: int, mount_path: str) -> str:
    host = dns_name.rstrip(".")
    default_port = 443 if protocol == "https" else 80
    port_suffix = "" if external_port == default_port else f":{external_port}"
    path_suffix = mount_path if mount_path.startswith("/") else f"/{mount_path}"
    return f"{protocol}://{host}{port_suffix}{path_suffix}"


def _collect_entries_from_maps(
    tcp_map: Any,
    web_map: Any,
    default_dns_name: str,
    *,
    foreground: bool,
) -> list[PreviewEntry]:
    if not isinstance(web_map, dict):
        return []
    safe_tcp = tcp_map if isinstance(tcp_map, dict) else {}
    entries: list[PreviewEntry] = []

    for host_port, web_cfg in sorted(web_map.items()):
        if not isinstance(web_cfg, dict):
            continue
        host, port_int = _split_host_port(host_port, default_dns_name)
        tcp_cfg = safe_tcp.get(str(port_int))
        if not isinstance(tcp_cfg, dict):
            tcp_cfg = {}
        protocol = "https" if tcp_cfg.get("HTTPS") else "http"
        handlers = web_cfg.get("Handlers") or {}
        if not isinstance(handlers, dict):
            continue
        for mount_path, handler in sorted(handlers.items()):
            if not isinstance(handler, dict):
                continue
            target = str(handler.get("Proxy") or handler.get("Path") or handler.get("Text") or "").strip()
            url = format_preview_url(protocol, host, port_int, mount_path)
            entries.append(
                PreviewEntry(
                    protocol=protocol,
                    external_port=port_int,
                    mount_path=mount_path,
                    target=target,
                    url=url,
                    foreground=foreground,
                )
            )
    return entries


def parse_serve_status_json(
    payload: dict[str, Any] | None,
    default_dns_name: str = "",
) -> list[PreviewEntry]:
    """Parse `tailscale serve status --json` into a sorted list of active PreviewEntry items."""
    if not payload or not isinstance(payload, dict):
        return []

    entries = _collect_entries_from_maps(
        payload.get("TCP"),
        payload.get("Web"),
        default_dns_name,
        foreground=False,
    )

    fg_map = payload.get("Foreground")
    if isinstance(fg_map, dict):
        seen = {(e.protocol, e.external_port, e.mount_path, e.foreground) for e in entries}
        for _session_id, fg_cfg in sorted(fg_map.items()):
            if not isinstance(fg_cfg, dict):
                continue
            for fg_entry in _collect_entries_from_maps(
                fg_cfg.get("TCP"),
                fg_cfg.get("Web"),
                default_dns_name,
                foreground=True,
            ):
                key = (fg_entry.protocol, fg_entry.external_port, fg_entry.mount_path, fg_entry.foreground)
                if key not in seen:
                    seen.add(key)
                    entries.append(fg_entry)

    entries.sort(key=lambda e: (e.external_port, e.mount_path, e.protocol, e.foreground))
    return entries


def _split_host_port(host_port: str, default_dns_name: str) -> tuple[str, int]:
    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        try:
            return host.rstrip("."), int(port_str)
        except ValueError:
            pass
    return (host_port or default_dns_name).rstrip("."), 443


def _is_https_unavailable_error(enable_url: str | None, err_msg: str) -> bool:
    if enable_url is not None:
        return True
    lower = err_msg.lower()
    return (
        "serve is not enabled" in lower
        or "https is not enabled" in lower
        or "https certificates are not enabled" in lower
        or "certdomains" in lower
        or ("https" in lower and "cert" in lower and ("disabled" in lower or "not enabled" in lower))
    )


def _is_benign_missing_serve_error(err_msg: str) -> bool:
    lower = err_msg.lower()
    if not lower:
        return True
    benign_phrases = (
        "no serve config",
        "not found",
        "does not exist",
        "nothing to remove",
        "no listener",
        "not configured",
    )
    return any(phrase in lower for phrase in benign_phrases)


class TailscalePreviewManager:
    """Controller for inspecting, starting, and stopping Tailscale Serve mobile previews."""

    def __init__(
        self,
        runner: SubprocessRunner | None = None,
        tailscale_bin: str = DEFAULT_TAILSCALE_BIN,
        telegram_send_bin: str = DEFAULT_TELEGRAM_SEND_BIN,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.tailscale_bin = tailscale_bin
        self.telegram_send_bin = telegram_send_bin

    def get_tailscale_state(self) -> TailscaleState:
        proc = self.runner.run([self.tailscale_bin, "status", "--json"])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
            raise PreviewError(f"Failed to query tailscale status: {err}")
        try:
            status_data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise PreviewError(f"Invalid JSON from tailscale status: {exc}") from exc
        if not isinstance(status_data, dict):
            raise PreviewError("Unexpected non-object JSON from tailscale status.")

        self_node = status_data.get("Self") or {}
        hostname = str(self_node.get("HostName") or "").strip()
        dns_name = str(self_node.get("DNSName") or "").strip().rstrip(".")
        ips = self_node.get("TailscaleIPs") or []
        ipv4 = next((str(ip) for ip in ips if ":" not in str(ip)), "")
        cert_domains_raw = status_data.get("CertDomains") or []
        cert_domains = tuple(str(d).rstrip(".") for d in cert_domains_raw if d)

        operator_user = ""
        prefs_proc = self.runner.run([self.tailscale_bin, "debug", "prefs"])
        if prefs_proc.returncode == 0 and prefs_proc.stdout:
            try:
                prefs_data = json.loads(prefs_proc.stdout)
                if isinstance(prefs_data, dict):
                    operator_user = str(prefs_data.get("OperatorUser") or "").strip()
            except json.JSONDecodeError:
                operator_user = ""

        if not dns_name:
            raise PreviewError(
                "Tailscale MagicDNS name is empty. Verify `tailscale status` is connected on this machine."
            )

        return TailscaleState(
            hostname=hostname,
            dns_name=dns_name,
            ipv4=ipv4,
            cert_domains=cert_domains,
            operator_user=operator_user,
        )

    def list_previews(self, state: TailscaleState | None = None) -> list[PreviewEntry]:
        dns_name = state.dns_name if state else ""
        if not dns_name:
            try:
                dns_name = self.get_tailscale_state().dns_name
            except PreviewError:
                dns_name = ""

        proc = self.runner.run([self.tailscale_bin, "serve", "status", "--json"])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
            raise PreviewError(f"Failed to read tailscale serve status: {err}")

        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PreviewError(f"Invalid JSON from tailscale serve status: {exc}") from exc
        if payload is None:
            return []
        if not isinstance(payload, dict):
            raise PreviewError("Unexpected non-object JSON from tailscale serve status.")

        return parse_serve_status_json(payload, default_dns_name=dns_name)

    def start_preview(
        self,
        raw_target: str,
        *,
        external_port: int | None = None,
        mount_path: str = "/",
        scheme_mode: str = "auto",
        notify_telegram: bool = False,
        label: str = "",
        pane_id: str = "",
    ) -> ServeResult:
        if scheme_mode not in {"auto", "https", "http"}:
            raise PreviewError(f"Unsupported scheme mode '{scheme_mode}'. Choose auto, https, or http.")

        parsed_target = parse_target_spec(raw_target)
        norm_path = normalize_mount_path(mount_path)
        ext_port = external_port if external_port is not None else parsed_target.local_port
        _validate_port(ext_port)

        state = self.get_tailscale_state()
        target, listening = resolve_reachable_target(parsed_target)

        fell_back = False
        enable_url: str | None = None

        if scheme_mode == "http":
            chosen_proto = "http"
            self._invoke_serve("http", ext_port, norm_path, target.url)
        elif scheme_mode == "https":
            chosen_proto = "https"
            ok, enable_url, err_msg = self._try_invoke_serve("https", ext_port, norm_path, target.url)
            if not ok:
                hint = (
                    f" Enable HTTPS certificates in the Tailscale admin console ({enable_url}) "
                    "or pass `--http` to serve over tailnet HTTP."
                    if enable_url
                    else ""
                )
                raise PreviewError(f"Failed to start HTTPS preview on port {ext_port}: {err_msg}.{hint}")
        else:
            existing = self.list_previews(state=state)
            has_existing_https = any(e.external_port == ext_port and e.protocol == "https" for e in existing)
            if state.https_ready:
                ok, enable_url, err_msg = self._try_invoke_serve("https", ext_port, norm_path, target.url)
                if ok:
                    chosen_proto = "https"
                else:
                    # Inspect fresh config: check if route applied or if any HTTPS sibling appeared concurrently
                    post_entries = self.list_previews(state=state)
                    applied_https = any(
                        e.external_port == ext_port
                        and e.protocol == "https"
                        and not e.foreground
                        and e.mount_path == norm_path
                        and e.target == target.url
                        for e in post_entries
                    )
                    post_has_https = any(e.external_port == ext_port and e.protocol == "https" for e in post_entries)
                    if applied_https:
                        chosen_proto = "https"
                    elif has_existing_https or post_has_https or not _is_https_unavailable_error(enable_url, err_msg):
                        raise PreviewError(f"Failed to start HTTPS preview on port {ext_port}: {err_msg}")
                    else:
                        fell_back = True
                        chosen_proto = "http"
                        self._invoke_serve("http", ext_port, norm_path, target.url)
            else:
                if has_existing_https:
                    raise PreviewError(
                        f"Port {ext_port} already has active HTTPS routes; refusing to downgrade it to HTTP."
                    )
                fell_back = True
                chosen_proto = "http"
                self._invoke_serve("http", ext_port, norm_path, target.url)

        url = format_preview_url(chosen_proto, state.dns_name, ext_port, norm_path)
        entry = PreviewEntry(
            protocol=chosen_proto,
            external_port=ext_port,
            mount_path=norm_path,
            target=target.url,
            url=url,
        )

        telegram_sent = False
        if notify_telegram:
            self.send_telegram_notification(entry, label=label, pane_id=pane_id)
            telegram_sent = True

        return ServeResult(
            entry=entry,
            fell_back_to_http=fell_back,
            enable_https_url=enable_url,
            local_port_listening=listening,
            telegram_sent=telegram_sent,
        )

    def _invoke_serve(self, proto: str, ext_port: int, mount_path: str, target_url: str) -> None:
        ok, enable_url, err_msg = self._try_invoke_serve(proto, ext_port, mount_path, target_url)
        if not ok:
            suffix = f" (enable at {enable_url})" if enable_url else ""
            raise PreviewError(f"tailscale serve failed: {err_msg}{suffix}")

    def _try_invoke_serve(
        self,
        proto: str,
        ext_port: int,
        mount_path: str,
        target_url: str,
    ) -> tuple[bool, str | None, str]:
        cmd = [self.tailscale_bin, "serve", "--bg", f"--{proto}={ext_port}"]
        if mount_path != "/":
            cmd.append(f"--set-path={mount_path}")
        cmd.append(target_url)

        try:
            proc = self.runner.run(cmd, timeout=DEFAULT_SERVE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return (
                False,
                None,
                "timed out waiting for tailscale serve",
            )

        combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        match = ENABLE_URL_RE.search(combined)
        enable_url = match.group(0) if match else None

        if proc.returncode != 0 or "Serve is not enabled on your tailnet" in combined:
            if "permission denied" in combined.lower() or "access denied" in combined.lower():
                try:
                    fallback_user = getpass.getuser()
                except Exception:
                    fallback_user = "$USER"
                user = os.environ.get("USER") or fallback_user
                return (
                    False,
                    enable_url,
                    f"{combined} (run once: sudo tailscale set --operator={user})",
                )
            return False, enable_url, combined or f"exit code {proc.returncode}"

        return True, enable_url, ""

    def stop_preview(
        self,
        target_port: int | None = None,
        *,
        mount_path: str | None = None,
    ) -> list[PreviewEntry]:
        """Stop a specific port/path preview or reset all active previews if target_port is None."""
        active = self.list_previews()
        if target_port is None:
            proc = self.runner.run([self.tailscale_bin, "serve", "reset"])
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
                raise PreviewError(f"Failed to reset tailscale serve: {err}")
            still_active = self.list_previews()
            still_fg = [e for e in still_active if e.foreground]
            if still_fg:
                urls = ", ".join(e.url for e in still_fg)
                raise PreviewError(
                    f"Cleared background previews, but foreground `tailscale serve` session(s) remain active ({urls}). "
                    "Stop those foreground terminal processes directly (Ctrl+C)."
                )
            if still_active:
                urls = ", ".join(e.url for e in still_active)
                raise PreviewError(f"Failed to clear all Tailscale Serve routes; still active: {urls}")
            return [e for e in active if not e.foreground]

        _validate_port(target_port)
        norm_path = normalize_mount_path(mount_path) if mount_path is not None else None
        matching = [
            entry
            for entry in active
            if entry.external_port == target_port and (norm_path is None or entry.mount_path == norm_path)
        ]

        fg_matching = [e for e in matching if e.foreground]
        if fg_matching:
            raise PreviewError(
                f"Preview {fg_matching[0].url} is running in a foreground `tailscale serve` session. "
                "Stop that terminal process directly (Ctrl+C) rather than `ts-preview off`."
            )

        if not matching:
            # Attempt idempotent off calls in case state changed, surfacing non-benign errors
            errors: list[str] = []
            for proto in ("https", "http"):
                cmd = [self.tailscale_bin, "serve", f"--{proto}={target_port}"]
                if norm_path is not None:
                    cmd.append(f"--set-path={norm_path}")
                cmd.append("off")
                proc = self.runner.run(cmd)
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout).strip()
                    if not _is_benign_missing_serve_error(err):
                        errors.append(err or f"exit code {proc.returncode}")
            if len(errors) == 2:
                raise PreviewError(f"Failed to stop preview on port {target_port}: {errors[0]}")
        elif norm_path is None:
            # Removing an entire port: run once per protocol without --set-path so sibling handlers do not double-delete
            for proto in sorted({entry.protocol for entry in matching}):
                cmd = [self.tailscale_bin, "serve", f"--{proto}={target_port}", "off"]
                proc = self.runner.run(cmd)
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
                    raise PreviewError(f"Failed to stop preview on {proto} port {target_port}: {err}")
        else:
            # Removing a specific mount_path (including explicit '/'): always pass --set-path so sibling paths survive
            for entry in matching:
                cmd = [
                    self.tailscale_bin,
                    "serve",
                    f"--{entry.protocol}={entry.external_port}",
                    f"--set-path={entry.mount_path}",
                    "off",
                ]
                proc = self.runner.run(cmd)
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
                    raise PreviewError(
                        f"Failed to stop preview on {entry.protocol} port {entry.external_port} ({entry.mount_path}): {err}"
                    )

        remaining = [
            entry
            for entry in self.list_previews()
            if entry.external_port == target_port and (norm_path is None or entry.mount_path == norm_path)
        ]
        if remaining:
            fg_rem = [e for e in remaining if e.foreground]
            if fg_rem:
                raise PreviewError(
                    f"Preview {fg_rem[0].url} is running in a foreground `tailscale serve` session. "
                    "Stop that terminal process directly (Ctrl+C) rather than `ts-preview off`."
                )
            urls = ", ".join(e.url for e in remaining)
            raise PreviewError(f"Preview remained active after `tailscale serve ... off`: {urls}")

        return matching

    def send_telegram_notification(
        self,
        entry: PreviewEntry,
        *,
        label: str = "",
        pane_id: str = "",
    ) -> None:
        title = f"Mobile preview ({label.strip()})" if label.strip() else "Mobile preview ready"
        stop_cmd = (
            f"ts-preview off {entry.external_port}"
            if entry.mount_path == "/"
            else f"ts-preview off {entry.external_port} --path {entry.mount_path}"
        )
        message = f"{title}: {entry.url}\nTarget: {entry.target}\nStop: {stop_cmd}"
        cmd = [self.telegram_send_bin]
        effective_pane = (pane_id or os.environ.get("HERDR_PANE_ID", "")).strip()
        if effective_pane:
            cmd.extend(["--pane", effective_pane])
        cmd.append(message)

        proc = self.runner.run(cmd, timeout=15.0)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
            raise PreviewError(f"Preview started at {entry.url}, but telegram-send failed: {err}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ts-preview",
        description=(
            "Expose a local dev server over Tailscale Serve for one-tap mobile testing "
            "on your tailnet, inspect active previews, or push the link to Telegram."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="External Tailscale port to expose or stop (defaults to the local target port)",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="URL mount path on the tailnet node (default: / when starting)",
    )
    scheme_group = parser.add_mutually_exclusive_group()
    scheme_group.add_argument(
        "--https",
        action="store_true",
        help="Require HTTPS (fails if tailnet HTTPS certs are not enabled)",
    )
    scheme_group.add_argument(
        "--http",
        action="store_true",
        help="Use plain HTTP over the encrypted WireGuard tailnet",
    )
    parser.add_argument(
        "-t",
        "--telegram",
        action="store_true",
        help="Send the clickable preview URL to Telegram via telegram-send",
    )
    parser.add_argument(
        "-l",
        "--label",
        default="",
        help="Optional label included in the Telegram message",
    )
    parser.add_argument(
        "--pane",
        default="",
        help="Optional Herdr pane ID to bind Telegram swipe-replies (defaults to $HERDR_PANE_ID)",
    )
    parser.add_argument(
        "args",
        nargs="*",
        help=(
            "Command or target: `<port|url>` to start (e.g. `3000`, `5173`), "
            "`status` (default when no args), `off [port]`, `reset`, or `doctor`"
        ),
    )
    return parser


def _print_status(
    manager: TailscalePreviewManager,
    *,
    as_json: bool = False,
    include_doctor: bool = False,
) -> int:
    state = manager.get_tailscale_state()
    previews = manager.list_previews(state=state)
    current_user = os.environ.get("USER", "")
    operator_ok = state.operator_user == current_user or os.geteuid() == 0

    if as_json:
        payload: dict[str, Any] = {
            "hostname": state.hostname,
            "dns_name": state.dns_name,
            "ipv4": state.ipv4,
            "operator_user": state.operator_user,
            "operator_ok": operator_ok,
            "https_ready": state.https_ready,
            "cert_domains": list(state.cert_domains),
            "previews": [
                {
                    "protocol": p.protocol,
                    "external_port": p.external_port,
                    "mount_path": p.mount_path,
                    "target": p.target,
                    "url": p.url,
                    "foreground": p.foreground,
                }
                for p in previews
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    if include_doctor:
        print(f"Node:          {state.dns_name} ({state.ipv4})")
        op_label = (
            f"{state.operator_user} (OK)"
            if operator_ok
            else f"{state.operator_user or 'unset'} (needs: sudo tailscale set --operator={current_user})"
        )
        print(f"Operator:      {op_label}")
        if state.https_ready:
            print(f"HTTPS certs:   enabled ({', '.join(state.cert_domains)})")
        else:
            print(
                "HTTPS certs:   disabled on tailnet (CertDomains: null). "
                "ts-preview automatically falls back to HTTP over WireGuard, or enable HTTPS at "
                "https://login.tailscale.com/admin/dns"
            )
        print()

    if not previews:
        print(f"No active Tailscale previews on {state.dns_name}.")
        return 0

    print(f"Active Tailscale previews on {state.dns_name}:")
    for entry in previews:
        if entry.foreground:
            print(f"  {entry.url} -> {entry.target}  [foreground session; stop with Ctrl+C in its terminal]")
        else:
            stop_hint = (
                f"ts-preview off {entry.external_port}"
                if entry.mount_path == "/"
                else f"ts-preview off {entry.external_port} --path {entry.mount_path}"
            )
            print(f"  {entry.url} -> {entry.target}  (stop: {stop_hint})")
    return 0


def main(argv: Sequence[str] | None = None, manager: TailscalePreviewManager | None = None) -> int:
    parser = _build_parser()
    parsed = parser.parse_args(list(argv) if argv is not None else None)
    mgr = manager or TailscalePreviewManager()

    positional = list(parsed.args)
    cmd = positional[0].lower() if positional else "status"

    try:
        if cmd in {"status", "ls", "list"}:
            if len(positional) > 1:
                raise PreviewError(f"Unexpected arguments after '{cmd}': {' '.join(positional[1:])}")
            return _print_status(mgr, as_json=parsed.json, include_doctor=False)

        if cmd in {"doctor", "check"}:
            if len(positional) > 1:
                raise PreviewError(f"Unexpected arguments after '{cmd}': {' '.join(positional[1:])}")
            return _print_status(mgr, as_json=parsed.json, include_doctor=True)

        if cmd in {"reset", "clear"}:
            if len(positional) > 1 or parsed.port is not None or parsed.path is not None:
                raise PreviewError(
                    "`ts-preview reset` clears all background previews and does not accept port or --path filters. "
                    "Use `ts-preview off <port> [--path /subpath]` for scoped teardown."
                )
            removed = mgr.stop_preview(None)
            if parsed.json:
                print(json.dumps({"stopped": [e.url for e in removed]}, indent=2))
            elif removed:
                print(f"Stopped {len(removed)} preview(s):")
                for entry in removed:
                    print(f"  {entry.url}")
            else:
                print("No active previews to reset.")
            return 0

        if cmd in {"off", "stop", "down"}:
            if parsed.https or parsed.http or parsed.telegram:
                raise PreviewError("`ts-preview off` does not accept --http, --https, or --telegram.")
            target_port: int | None
            if len(positional) == 2:
                if not positional[1].isdigit():
                    raise PreviewError(f"Port to stop must be an integer (got '{positional[1]}').")
                pos_port = int(positional[1])
                if parsed.port is not None and parsed.port != pos_port:
                    raise PreviewError(f"Conflicting ports given: '{pos_port}' and '--port {parsed.port}'.")
                target_port = pos_port
            elif len(positional) == 1:
                if parsed.port is not None:
                    target_port = parsed.port
                elif parsed.path is not None:
                    raise PreviewError(
                        "Specify a port when stopping a specific --path (e.g. `ts-preview off 3000 --path /app`), "
                        "or run `ts-preview reset` to clear all previews."
                    )
                else:
                    target_port = None
            else:
                raise PreviewError("Usage: ts-preview off [port] [--path /subpath]")

            mount_filter = normalize_mount_path(parsed.path) if parsed.path is not None else None
            removed = mgr.stop_preview(target_port, mount_path=mount_filter)

            if parsed.json:
                print(json.dumps({"stopped": [e.url for e in removed]}, indent=2))
            elif removed:
                for entry in removed:
                    print(f"Stopped preview: {entry.url}")
            else:
                print("No matching active preview found (serve state is clean).")
            return 0

        # Otherwise treat positional[0] (or `on`/`start` + positional[1]) as target to serve
        if cmd in {"on", "start", "serve", "up"}:
            if len(positional) != 2:
                raise PreviewError(f"Usage: ts-preview {cmd} <port|localhost-url>")
            raw_target = positional[1]
        else:
            if len(positional) != 1:
                raise PreviewError("Usage: ts-preview <port|localhost-url> [--telegram] [--http|--https]")
            raw_target = positional[0]

        scheme_mode = "https" if parsed.https else ("http" if parsed.http else "auto")
        effective_path = parsed.path if parsed.path is not None else "/"
        result = mgr.start_preview(
            raw_target,
            external_port=parsed.port,
            mount_path=effective_path,
            scheme_mode=scheme_mode,
            notify_telegram=parsed.telegram,
            label=parsed.label,
            pane_id=parsed.pane,
        )

        if parsed.json:
            print(
                json.dumps(
                    {
                        "url": result.entry.url,
                        "protocol": result.entry.protocol,
                        "external_port": result.entry.external_port,
                        "mount_path": result.entry.mount_path,
                        "target": result.entry.target,
                        "fell_back_to_http": result.fell_back_to_http,
                        "enable_https_url": result.enable_https_url,
                        "local_port_listening": result.local_port_listening,
                        "telegram_sent": result.telegram_sent,
                    },
                    indent=2,
                )
            )
            return 0

        stop_cmd = (
            f"ts-preview off {result.entry.external_port}"
            if result.entry.mount_path == "/"
            else f"ts-preview off {result.entry.external_port} --path {result.entry.mount_path}"
        )
        print(f"Preview URL: {result.entry.url}")
        print(f"Target:      {result.entry.target}")
        print(f"Stop with:   {stop_cmd}")
        if result.fell_back_to_http:
            hint_url = result.enable_https_url or "https://login.tailscale.com/admin/dns"
            print(
                f"Note:        Serving over tailnet HTTP (WireGuard-encrypted) because HTTPS certificates "
                f"are not enabled on your tailnet yet. Enable HTTPS once at {hint_url}"
            )
        if not result.local_port_listening:
            print(
                f"Warning:     Nothing is currently listening on local port "
                f"{parse_target_spec(raw_target).local_port} yet (proxy is ready when your dev server starts)."
            )
        if result.telegram_sent:
            print("Telegram:    Sent clickable preview link to your phone.")
        return 0

    except PreviewError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
