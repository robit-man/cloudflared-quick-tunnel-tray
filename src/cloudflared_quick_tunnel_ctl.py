#!/usr/bin/python3
"""Command-line client for the per-user Quick Tunnel broker."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any


SOCKET_PATH = Path("/run/cloudflared-quick-tunnel/control.sock")


def request(payload: dict[str, Any], timeout: float = 50.0) -> dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(SOCKET_PATH))
        connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise RuntimeError("Service response is too large")
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise RuntimeError("Quick Tunnel service is not running") from exc
    except socket.timeout as exc:
        raise RuntimeError("Quick Tunnel service did not respond in time") from exc
    finally:
        connection.close()
    if not data:
        raise RuntimeError("Quick Tunnel service closed the connection without a response")
    try:
        response = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Quick Tunnel service returned an invalid response") from exc
    if not isinstance(response, dict):
        raise RuntimeError("Quick Tunnel service returned an invalid response")
    return response


def add_json_option(parser: argparse.ArgumentParser, suppress_default: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_default else False
    parser.add_argument(
        "--json",
        action="store_true",
        default=default,
        help="print the complete JSON response",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudflared-quick-tunnelctl",
        description="Control the current Unix user's Cloudflare Quick Tunnel",
    )
    add_json_option(parser)
    subparsers = parser.add_subparsers(dest="action", required=True)

    for action in ("status", "stop", "ping"):
        command = subparsers.add_parser(action)
        add_json_option(command, suppress_default=True)

    for action in ("start", "cycle"):
        command = subparsers.add_parser(action)
        target = command.add_mutually_exclusive_group()
        target.add_argument(
            "--port",
            type=int,
            default=None,
            help="local TCP port (start defaults to 22; cycle keeps the current port)",
        )
        target.add_argument(
            "--browser", action="store_true", help="publish the browser terminal for local SSH",
        )
        add_json_option(command, suppress_default=True)
    return parser


def print_human(response: dict[str, Any], action: str) -> None:
    if action == "ping":
        print(f"service {response.get('version', 'unknown')}")
        return
    if action in {"start", "cycle"} and response.get("endpoint"):
        print(response.get("browser_url") or response["endpoint"])
        return
    state = response.get("state", "unknown")
    if state == "ready":
        target = "browser terminal (local SSH)" if response.get("mode") == "browser-tty" else f"localhost:{response['port']}"
        print(f"ready {response['endpoint']} -> {target}")
    elif state == "error":
        print(f"error: {response.get('last_error') or 'unknown error'}")
    else:
        print(state)


def main() -> int:
    args = build_parser().parse_args()
    payload: dict[str, Any] = {"action": args.action}
    if args.action in {"start", "cycle"}:
        if args.browser:
            payload["mode"] = "browser-tty"
        elif args.port is not None:
            payload["port"] = args.port
    try:
        response = request(payload)
    except RuntimeError as exc:
        print(f"cloudflared-quick-tunnelctl: {exc}", file=sys.stderr)
        return 1
    if not response.get("ok"):
        print(
            f"cloudflared-quick-tunnelctl: {response.get('error', 'request failed')}",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps(response, indent=2, sort_keys=True))
    else:
        print_human(response, args.action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
