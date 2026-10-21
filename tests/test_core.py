#!/usr/bin/python3

from __future__ import annotations

import importlib.util
import socket
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
