#!/usr/bin/python3

from __future__ import annotations

import importlib.util
import io
import socket
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


daemon = load_module("quick_tunnel_daemon", ROOT / "src/cloudflared_quick_tunnel_daemon.py")
ctl = load_module("quick_tunnel_ctl", ROOT / "src/cloudflared_quick_tunnel_ctl.py")


class DaemonCoreTests(unittest.TestCase):
    def test_endpoint_extraction(self) -> None:
        line = "\x1b[32mINF\x1b[0m | https://Example-Tunnel.trycloudflare.com |"
        clean = daemon.ANSI_RE.sub("", line)
        match = daemon.ENDPOINT_RE.search(clean)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).lower(), "example-tunnel.trycloudflare.com")

    def test_port_validation(self) -> None:
        self.assertEqual(daemon.validate_port("22"), 22)
        for invalid in (0, 65536, True, "not-a-port", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(daemon.RequestError):
                    daemon.validate_port(invalid)

    def test_open_port_detection(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            self.assertTrue(daemon.local_port_is_open(port))
        finally:
            listener.close()

    def test_cycle_keeps_browser_mode_unless_explicitly_overridden(self) -> None:
        session = daemon.TunnelSession(1000, 1000, "test", mode=daemon.BROWSER_MODE, port=22)
        with patch.object(daemon.MANAGER, "session_for_peer", return_value=session), patch.object(
            session, "start", return_value={}
        ) as start:
            daemon.handle_request(1000, 1000, {"action": "cycle"})
            daemon.handle_request(1000, 1000, {"action": "cycle", "port": 8080})
            self.assertEqual(start.call_args_list[0].kwargs["mode"], daemon.BROWSER_MODE)
            self.assertEqual(start.call_args_list[1].kwargs["mode"], daemon.NATIVE_MODE)

    def test_browser_mode_cannot_run_as_root(self) -> None:
        session = daemon.TunnelSession(0, 0, "root")
        with self.assertRaisesRegex(daemon.RequestError, "non-root"):
            session.start(22, force=False, mode=daemon.BROWSER_MODE)

    def test_gateway_exit_stops_cloudflared_and_revokes_link(self) -> None:
        gateway = Mock(returncode=1)
        gateway.poll.return_value = 1
        process = Mock(pid=123)
        process.poll.return_value = None
        session = daemon.TunnelSession(
            1000, 1000, "test", mode=daemon.BROWSER_MODE, port=22, process=process,
            gateway=gateway, state="ready", endpoint="test.trycloudflare.com", token="token",
        )
        with patch.object(session, "_terminate_process_locked") as terminate, self.assertLogs(daemon.LOG):
            response = session.status()
        terminate.assert_called_once_with(process)
        self.assertEqual(response["state"], "error")
        self.assertIsNone(response["browser_url"])
        self.assertIsNone(session.token)


class CliParserTests(unittest.TestCase):
    def test_json_option_before_or_after_subcommand(self) -> None:
        for argv in (["--json", "status"], ["status", "--json"]):
            with self.subTest(argv=argv):
                args = ctl.build_parser().parse_args(argv)
                self.assertTrue(args.json)

    def test_cycle_port(self) -> None:
        args = ctl.build_parser().parse_args(["cycle", "--port", "2222"])
        self.assertEqual(args.action, "cycle")
        self.assertEqual(args.port, 2222)

    def test_browser_flags_and_link_output(self) -> None:
        for action in ("start", "cycle"):
            self.assertTrue(ctl.build_parser().parse_args([action, "--browser"]).browser)
        endpoint = "test.trycloudflare.com"
        url = f"https://{endpoint}/#tty=token"
        with redirect_stdout(io.StringIO()) as output:
            ctl.print_human({"endpoint": endpoint, "browser_url": url}, "start")
        self.assertEqual(output.getvalue().strip(), url)


if __name__ == "__main__":
    unittest.main()
