#!/usr/bin/env python3
"""Unit tests for ts_preview."""

from __future__ import annotations

import io
import json
import subprocess
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ts_preview import (
    PreviewError,
    SubprocessRunner,
    TailscalePreviewManager,
    format_preview_url,
    main,
    normalize_mount_path,
    parse_serve_status_json,
    parse_target_spec,
)


class FakeRunner(SubprocessRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.status_json: dict[str, object] = {
            "Self": {
                "HostName": "devbox",
                "DNSName": "devbox.tail00000.ts.net.",
                "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
            },
            "CertDomains": None,
        }
        self.prefs_json: dict[str, object] = {"OperatorUser": "devuser"}
        self.serve_status_json: dict[str, object] = {}
        self.https_fail_message: str | None = (
            "Serve is not enabled on your tailnet.\n"
            "To enable, visit:\n\n"
            "         https://login.tailscale.com/f/serve?node=nVw9hJkmii11CNTRL\n"
        )
        self.keep_route_on_off: bool = False
        self.on_https_attempt: object | None = None
        self.telegram_rc: int = 0

    def _apply_off(self, cmd: list[str]) -> None:
        if self.keep_route_on_off or not isinstance(self.serve_status_json, dict):
            return
        port_str: str | None = None
        set_path: str | None = None
        for arg in cmd:
            if arg.startswith("--http=") or arg.startswith("--https="):
                port_str = arg.split("=", 1)[1]
            elif arg.startswith("--set-path="):
                set_path = arg.split("=", 1)[1]
        if not port_str:
            return
        web_map = self.serve_status_json.get("Web")
        tcp_map = self.serve_status_json.get("TCP")
        if not isinstance(web_map, dict):
            return
        for host_port in list(web_map.keys()):
            if not host_port.endswith(f":{port_str}"):
                continue
            if set_path is None:
                web_map.pop(host_port, None)
                if isinstance(tcp_map, dict):
                    tcp_map.pop(port_str, None)
            else:
                handlers = web_map.get(host_port, {}).get("Handlers")
                if isinstance(handlers, dict):
                    handlers.pop(set_path, None)
                    if not handlers:
                        web_map.pop(host_port, None)
                        if isinstance(tcp_map, dict):
                            tcp_map.pop(port_str, None)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 8.0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = list(argv)
        self.calls.append(cmd)

        if cmd[:2] == ["tailscale", "status"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.status_json), "")
        if cmd[:3] == ["tailscale", "debug", "prefs"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.prefs_json), "")
        if cmd[:3] == ["tailscale", "serve", "status"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.serve_status_json), "")
        if cmd[:3] == ["tailscale", "serve", "reset"]:
            if isinstance(self.serve_status_json, dict) and not self.keep_route_on_off:
                fg = self.serve_status_json.get("Foreground")
                self.serve_status_json = {"Foreground": fg} if fg else {}
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "tailscale" and cmd[1] == "serve":
            if cmd[-1] == "off":
                self._apply_off(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if any(arg.startswith("--https=") for arg in cmd):
                if callable(self.on_https_attempt):
                    self.on_https_attempt()
                if self.https_fail_message:
                    return subprocess.CompletedProcess(cmd, 1, self.https_fail_message, "")
            return subprocess.CompletedProcess(cmd, 0, "Serve started", "")
        if cmd[0].endswith("telegram-send"):
            if self.telegram_rc != 0:
                return subprocess.CompletedProcess(cmd, self.telegram_rc, "", "telegram failure")
            return subprocess.CompletedProcess(cmd, 0, "Sent Telegram message", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


class TsPreviewUnitTests(unittest.TestCase):
    def test_parse_target_spec_accepts_port_and_localhost_urls(self) -> None:
        spec1 = parse_target_spec("3000")
        self.assertEqual(spec1.url, "http://127.0.0.1:3000")
        self.assertEqual(spec1.local_port, 3000)

        spec2 = parse_target_spec("localhost:5173")
        self.assertEqual(spec2.url, "http://127.0.0.1:5173")
        self.assertEqual(spec2.local_port, 5173)

        spec3 = parse_target_spec("https+insecure://127.0.0.1:8443/api")
        self.assertEqual(spec3.url, "https+insecure://127.0.0.1:8443/api")
        self.assertEqual(spec3.local_port, 8443)

    def test_parse_target_spec_rejects_non_local_or_invalid_ports(self) -> None:
        with self.assertRaises(PreviewError):
            parse_target_spec("example.com:3000")
        with self.assertRaises(PreviewError):
            parse_target_spec("0.0.0.0:3000")
        with self.assertRaises(PreviewError):
            parse_target_spec("70000")
        with self.assertRaises(PreviewError):
            parse_target_spec("ftp://localhost:3000")

    def test_normalize_mount_path(self) -> None:
        self.assertEqual(normalize_mount_path(""), "/")
        self.assertEqual(normalize_mount_path("app"), "/app")
        self.assertEqual(normalize_mount_path("app/"), "/app/")
        self.assertEqual(normalize_mount_path("/docs/"), "/docs/")
        self.assertEqual(normalize_mount_path("/v1/ui"), "/v1/ui")
        with self.assertRaises(PreviewError):
            normalize_mount_path("/bad?query=1")

    def test_parse_serve_status_json_http_and_https(self) -> None:
        payload = {
            "TCP": {
                "443": {"HTTPS": True},
                "3000": {"HTTP": True},
            },
            "Web": {
                "devbox.tail00000.ts.net:443": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:5173"},
                    }
                },
                "devbox.tail00000.ts.net:3000": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:3000"},
                    }
                },
            },
        }
        entries = parse_serve_status_json(payload)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].url, "https://devbox.tail00000.ts.net/")
        self.assertEqual(entries[0].protocol, "https")
        self.assertEqual(entries[0].external_port, 443)
        self.assertEqual(entries[1].url, "http://devbox.tail00000.ts.net:3000/")
        self.assertEqual(entries[1].protocol, "http")
        self.assertEqual(entries[1].external_port, 3000)

    def test_auto_mode_uses_http_when_cert_domains_null(self) -> None:
        runner = FakeRunner()
        mgr = TailscalePreviewManager(runner=runner, telegram_send_bin="/bin/telegram-send")
        with patch("ts_preview.is_local_port_listening", return_value=True):
            res = mgr.start_preview("5173", notify_telegram=True, label="vite", pane_id="w22:p1")

        self.assertTrue(res.fell_back_to_http)
        self.assertEqual(res.entry.protocol, "http")
        self.assertEqual(res.entry.url, "http://devbox.tail00000.ts.net:5173/")
        self.assertIn(
            ["tailscale", "serve", "--bg", "--http=5173", "http://127.0.0.1:5173"],
            runner.calls,
        )
        tg_calls = [c for c in runner.calls if c[0] == "/bin/telegram-send"]
        self.assertEqual(len(tg_calls), 1)
        self.assertEqual(tg_calls[0][:3], ["/bin/telegram-send", "--pane", "w22:p1"])
        self.assertIn("http://devbox.tail00000.ts.net:5173/", tg_calls[0][3])

    def test_auto_mode_uses_https_when_cert_domains_present(self) -> None:
        runner = FakeRunner()
        runner.status_json["CertDomains"] = ["devbox.tail00000.ts.net"]
        runner.https_fail_message = None
        mgr = TailscalePreviewManager(runner=runner)
        with patch("ts_preview.is_local_port_listening", return_value=True):
            res = mgr.start_preview("3000")

        self.assertFalse(res.fell_back_to_http)
        self.assertEqual(res.entry.protocol, "https")
        self.assertEqual(res.entry.url, "https://devbox.tail00000.ts.net:3000/")
        self.assertIn(
            ["tailscale", "serve", "--bg", "--https=3000", "http://127.0.0.1:3000"],
            runner.calls,
        )

    def test_https_flag_surfaces_enable_url_when_certs_disabled(self) -> None:
        runner = FakeRunner()
        mgr = TailscalePreviewManager(runner=runner)
        err = io.StringIO()
        with patch("ts_preview.is_local_port_listening", return_value=True), redirect_stderr(err):
            rc = main(["--https", "3000"], manager=mgr)
        self.assertEqual(rc, 1)
        self.assertIn("https://login.tailscale.com/f/serve?node=nVw9hJkmii11CNTRL", err.getvalue())

    def test_stop_specific_port_only_removes_matching_entry(self) -> None:
        runner = FakeRunner()
        runner.serve_status_json = {
            "TCP": {
                "3000": {"HTTP": True},
                "5173": {"HTTP": True},
            },
            "Web": {
                "devbox.tail00000.ts.net:3000": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}},
                "devbox.tail00000.ts.net:5173": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5173"}}},
            },
        }
        mgr = TailscalePreviewManager(runner=runner)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["off", "3000"], manager=mgr)
        self.assertEqual(rc, 0)
        self.assertIn(["tailscale", "serve", "--http=3000", "off"], runner.calls)
        self.assertNotIn(["tailscale", "serve", "--http=5173", "off"], runner.calls)

    def test_format_preview_url_omits_default_ports(self) -> None:
        self.assertEqual(
            format_preview_url("https", "devbox.tail00000.ts.net.", 443, "/"),
            "https://devbox.tail00000.ts.net/",
        )
        self.assertEqual(
            format_preview_url("http", "devbox.tail00000.ts.net.", 80, "/app"),
            "http://devbox.tail00000.ts.net/app",
        )

    def test_off_flag_scoping_and_root_path_preserves_sibling_routes(self) -> None:
        runner = FakeRunner()
        runner.serve_status_json = {
            "TCP": {"3000": {"HTTP": True}, "5173": {"HTTP": True}},
            "Web": {
                "devbox.tail00000.ts.net:3000": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:3000"},
                        "/app": {"Proxy": "http://127.0.0.1:3001"},
                    }
                },
                "devbox.tail00000.ts.net:5173": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:5173"}},
                },
            },
        }
        mgr = TailscalePreviewManager(runner=runner)

        # 1. `off --path /app` without port and `reset --port 3000` fail closed instead of global reset
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["off", "--path", "/app"], manager=mgr), 1)
            self.assertEqual(main(["reset", "--port", "3000"], manager=mgr), 1)
        self.assertNotIn(["tailscale", "serve", "reset"], runner.calls)

        # 2. `off 3000 --path /` passes explicit `--set-path=/` so `/app` survives
        runner.calls.clear()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["off", "3000", "--path", "/"], manager=mgr), 0)
        self.assertIn(["tailscale", "serve", "--http=3000", "--set-path=/", "off"], runner.calls)
        self.assertNotIn(["tailscale", "serve", "--http=3000", "off"], runner.calls)

        # 3. `off --port 3000` honors `--port` (no global reset) and issues one port-level `off`
        runner.calls.clear()
        with redirect_stdout(out):
            self.assertEqual(main(["off", "--port", "3000"], manager=mgr), 0)
        self.assertNotIn(["tailscale", "serve", "reset"], runner.calls)
        port_offs = [c for c in runner.calls if c == ["tailscale", "serve", "--http=3000", "off"]]
        self.assertEqual(len(port_offs), 1)

    def test_null_serve_status_and_foreground_session_detection(self) -> None:
        self.assertEqual(parse_serve_status_json(None), [])

        runner = FakeRunner()
        runner.serve_status_json = None  # type: ignore[assignment]
        mgr = TailscalePreviewManager(runner=runner)
        self.assertEqual(mgr.list_previews(), [])

        # Foreground serve session is surfaced in status and rejected cleanly on `off`
        runner.serve_status_json = {
            "Foreground": {
                "sess123": {
                    "TCP": {"5173": {"HTTP": True}},
                    "Web": {
                        "devbox.tail00000.ts.net:5173": {
                            "Handlers": {"/": {"Proxy": "http://127.0.0.1:5173"}},
                        }
                    },
                }
            }
        }
        entries = mgr.list_previews()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].foreground)

        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(["off", "5173"], manager=mgr)
        self.assertEqual(rc, 1)
        self.assertIn("foreground", err.getvalue().lower())

    def test_https_auto_does_not_downgrade_on_unrelated_failure_or_existing_https_port(self) -> None:
        runner = FakeRunner()
        runner.status_json["CertDomains"] = ["devbox.tail00000.ts.net"]
        runner.https_fail_message = "access denied: operator mismatch"
        mgr = TailscalePreviewManager(runner=runner)
        with patch("ts_preview.is_local_port_listening", return_value=True):
            with self.assertRaises(PreviewError):
                mgr.start_preview("3000")
        self.assertFalse(any(any(a.startswith("--http=") for a in c) for c in runner.calls))

        # Existing HTTPS route on port 443 prevents HTTP downgrade even when CertDomains flips null
        runner2 = FakeRunner()
        runner2.status_json["CertDomains"] = None
        runner2.serve_status_json = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "devbox.tail00000.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}},
                }
            },
        }
        mgr2 = TailscalePreviewManager(runner=runner2)
        with patch("ts_preview.is_local_port_listening", return_value=True):
            with self.assertRaises(PreviewError):
                mgr2.start_preview("5173", external_port=443, mount_path="/vite")
        self.assertFalse(any(any(a.startswith("--http=") for a in c) for c in runner2.calls))

        # Concurrent HTTPS sibling appearing in fresh snapshot after failed HTTPS attempt prevents HTTP downgrade
        runner3 = FakeRunner()
        runner3.status_json["CertDomains"] = ["devbox.tail00000.ts.net"]

        def inject_concurrent_https() -> None:
            runner3.serve_status_json = {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "devbox.tail00000.ts.net:443": {
                        "Handlers": {"/other": {"Proxy": "http://127.0.0.1:4000"}},
                    }
                },
            }

        runner3.on_https_attempt = inject_concurrent_https
        mgr3 = TailscalePreviewManager(runner=runner3)
        with patch("ts_preview.is_local_port_listening", return_value=True):
            with self.assertRaises(PreviewError):
                mgr3.start_preview("5173", external_port=443, mount_path="/vite")
        self.assertFalse(any(any(a.startswith("--http=") for a in c) for c in runner3.calls))

    def test_trailing_slash_mounts_and_post_shutdown_verification(self) -> None:
        # 1. Background `/docs/` mount is matched and removed with exact `--set-path=/docs/` while `/docs` survives
        runner = FakeRunner()
        runner.serve_status_json = {
            "TCP": {"3000": {"HTTP": True}},
            "Web": {
                "devbox.tail00000.ts.net:3000": {
                    "Handlers": {
                        "/docs": {"Proxy": "http://127.0.0.1:3001"},
                        "/docs/": {"Proxy": "http://127.0.0.1:3002"},
                    }
                }
            },
        }
        mgr = TailscalePreviewManager(runner=runner)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["off", "3000", "--path", "/docs/"], manager=mgr), 0)
        self.assertIn(["tailscale", "serve", "--http=3000", "--set-path=/docs/", "off"], runner.calls)
        remaining = mgr.list_previews()
        self.assertEqual([e.mount_path for e in remaining], ["/docs"])

        # 2. Silent failed removal (route still present in post-off status) fails closed instead of claiming clean
        runner.keep_route_on_off = True
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["off", "3000", "--path", "/docs"], manager=mgr), 1)
        self.assertIn("remained active", err.getvalue().lower())

        # 3. Foreground `/docs/` mount is detected and rejected cleanly
        fg_runner = FakeRunner()
        fg_runner.serve_status_json = {
            "Foreground": {
                "sess1": {
                    "TCP": {"3000": {"HTTP": True}},
                    "Web": {
                        "devbox.tail00000.ts.net:3000": {
                            "Handlers": {"/docs/": {"Proxy": "http://127.0.0.1:3002"}},
                        }
                    },
                }
            }
        }
        fg_mgr = TailscalePreviewManager(runner=fg_runner)
        fg_err = io.StringIO()
        with redirect_stderr(fg_err):
            self.assertEqual(main(["off", "3000", "--path", "/docs/"], manager=fg_mgr), 1)
        self.assertIn("foreground", fg_err.getvalue().lower())

    def test_ipv6_only_localhost_listener_forwards_to_ipv6_loopback(self) -> None:
        runner = FakeRunner()
        mgr = TailscalePreviewManager(runner=runner)

        def fake_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
            return port == 5173 and host == "::1"

        with patch("ts_preview.is_local_port_listening", side_effect=fake_listening):
            res = mgr.start_preview("5173")

        self.assertTrue(res.local_port_listening)
        self.assertEqual(res.entry.target, "http://[::1]:5173")
        self.assertIn(
            ["tailscale", "serve", "--bg", "--http=5173", "http://[::1]:5173"],
            runner.calls,
        )


if __name__ == "__main__":
    unittest.main()
