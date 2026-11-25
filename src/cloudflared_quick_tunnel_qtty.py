#!/usr/bin/python3
"""qtty: terminal sessions that outlive SSH and browser connections.

Sessions live in a per-user holder process, so closing a browser, cycling a
Quick Tunnel, or dropping SSH only detaches them. Run qtty for a picker, or
qtty ls, new, attach and kill. Inside a session, Ctrl-] detaches.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty
from pathlib import Path

# The installed command and its holder use the same /usr or /usr/local prefix.
_program = Path(__file__).resolve()
for _directory in (_program.parent, _program.parent.parent / "libexec"):
    if (_directory / "cloudflared_quick_tunnel_qtty_holder.py").is_file():
        sys.path.insert(0, str(_directory))
        break
from cloudflared_quick_tunnel_qtty_holder import (  # noqa: E402
    DETACH_KEY, NAME_RE, OSC_SESSION, Frames, frame, runtime_dir, serve,
)


def start_holder() -> None:
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            for sig in (signal.SIGHUP, signal.SIGPIPE):
                signal.signal(sig, signal.SIG_IGN)
            try:
                asyncio.run(serve())
            finally:
                os._exit(0)
        os._exit(0)
    os.wait()


def connect(start: bool = True) -> socket.socket:
    path = runtime_dir() / "control"
    for attempt in range(40):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if len(os.fsencode(path)) >= 108:
            raise SystemExit(f"qtty: socket path {path} is too long; set QTTY_DIR to a shorter directory")
        try:
            conn.connect(str(path))
            return conn
        except (FileNotFoundError, ConnectionRefusedError):
            conn.close()
            if not start:
                raise
            if attempt == 0:
                start_holder()
            time.sleep(0.05)
    raise SystemExit("qtty: could not start the session holder")


def receive(conn: socket.socket, parser: Frames) -> list[tuple[bytes, bytes]]:
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            raise ConnectionError("qtty holder closed the connection")
        frames = parser.feed(chunk)
        if frames:
            return frames


def ask(request: dict, start: bool = True) -> dict:
    with connect(start) as conn:
        conn.sendall(frame(b"q", json.dumps(request).encode()))
        return json.loads(receive(conn, Frames())[0][1])


def sessions() -> list[dict]:
    try:
        return ask({"op": "list"}, start=False)["sessions"]
    except (FileNotFoundError, ConnectionRefusedError):
        return []


def free_name() -> str:
    used = {item["name"] for item in sessions()}
    return next(f"s{n}" for n in range(1, 1000) if f"s{n}" not in used)


def terminal_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size(sys.stdin.fileno())
        return size.columns, size.lines
    except OSError:
        return 80, 24


def attach(name: str, create: bool = False, cwd: str | None = None) -> str:
    """Bridge this terminal to a session. Returns why the attachment ended."""
    if os.environ.get("QTTY_SESSION"):
        print(f"qtty: already inside session {os.environ['QTTY_SESSION']}; detach with Ctrl-] first",
              file=sys.stderr)
        return "nested"
    cols, rows = terminal_size()
    conn = connect()
    request = {"op": "attach", "name": name, "create": create, "cols": cols, "rows": rows,
               "env": dict(os.environ), "cwd": cwd or os.getcwd()}
    conn.sendall(frame(b"q", json.dumps(request).encode()))
    parser = Frames()
    frames = receive(conn, parser)
    answer = json.loads(frames.pop(0)[1])
    if not answer.get("ok"):
        conn.close()
        print(f"qtty: {answer.get('error', 'attach failed')}", file=sys.stderr)
        return "missing"
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_write, False)
    previous = signal.signal(signal.SIGWINCH, lambda *_: os.write(wake_write, b"w"))
    saved = termios.tcgetattr(0) if os.isatty(0) else None
    if saved:
        tty.setraw(0)
    os.write(1, OSC_SESSION.format(name).encode())
    reason = "lost"
    try:
        pending = frames
        while True:
            for kind, payload in pending:
                if kind == b"d":
                    os.write(1, payload)
                elif kind == b"x":
                    return (reason := "exited")
                elif kind == b"k":
                    return (reason := "taken")
            pending = []
            ready = select.select([0, conn, wake_read], [], [])[0]
            if wake_read in ready:
                os.read(wake_read, 64)
                cols, rows = terminal_size()
                conn.sendall(frame(b"r", struct.pack("!HH", cols, rows)))
            if 0 in ready:
                data = os.read(0, 65536)
                if not data:
                    return (reason := "lost")
                before, key, _ = data.partition(DETACH_KEY)
                if before:
                    conn.sendall(frame(b"d", before))
                if key:
                    return (reason := "detached")
            if conn in ready:
                chunk = conn.recv(65536)
                if not chunk:
                    return (reason := "lost")
                pending = parser.feed(chunk)
    finally:
        conn.close()
        signal.signal(signal.SIGWINCH, previous)
        os.close(wake_read)
        os.close(wake_write)
        if saved:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
        messages = {"detached": "detached from", "exited": "session ended:", "taken": "attached elsewhere:"}
        note = messages.get(reason, "lost connection to")
        os.write(1, (OSC_SESSION.format("") + f"\x1b[0m\r\n[qtty: {note} {name}]\r\n").encode())


def ago(timestamp: float) -> str:
    seconds = max(0, int(time.time() - timestamp))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def read_key() -> bytes:
    return os.read(0, 32)


def prompt_name(default: str) -> str | None:
    typed = ""
    while True:
        os.write(1, f"\r\x1b[2KName for the new session [{default}]: {typed}".encode())
        key = read_key()
        if key in (b"\r", b"\n"):
            name = typed or default
            if NAME_RE.fullmatch(name):
                return name
            typed = ""
        elif key in (b"\x1b", b"\x03"):
            return None
        elif key in (b"\x7f", b"\x08"):
            typed = typed[:-1]
        elif len(key) == 1 and 32 < key[0] < 127:
            typed += key.decode()


def pick(login: bool) -> tuple[str, str | None]:
    """Arrow-key session picker. Returns (action, session name)."""
    selected = 0
    saved = termios.tcgetattr(0)
    tty.setraw(0)
    try:
        while True:
            listing = sessions()
            items = [("attach", item["name"]) for item in listing] + [("new", None), ("shell", None)]
            items.append(("quit", None))
            selected = min(selected, len(items) - 1)
            lines = ["\x1b[H\x1b[2J\x1b[1mqtty\x1b[0m  choose a terminal session", ""]
            if not listing:
                lines.append("  \x1b[2mNo saved sessions yet.\x1b[0m")
            for index, (action, name) in enumerate(items):
                if action == "attach":
                    item = listing[index]
                    detail = f"{item['command'] or '?':<12} active {ago(item['active'])} ago"
                    if item["attached"]:
                        detail += "  (open elsewhere)"
                    label = f"{name:<16} {detail}"
                else:
                    label = {"new": "+ New session", "shell": "$ Plain shell",
                             "quit": "Disconnect" if login else "Quit"}[action]
                label = label[:max(8, terminal_size()[0] - 6)]  # Never wrap in narrow panes.
                marker = "\x1b[7m" if index == selected else ""
                lines.append(f"  {marker} {label} \x1b[0m")
            lines += ["", "\x1b[2m  ↑/↓ select · Enter open · n new · x end session · q quit\x1b[0m",
                      "\x1b[2m  Inside a session, Ctrl-] returns here.\x1b[0m"]
            os.write(1, ("\r\n".join(lines) + "\r\n").encode())
            key = read_key()
            if key in (b"\x1b[A", b"\x1bOA", b"k"):
                selected = (selected - 1) % len(items)
            elif key in (b"\x1b[B", b"\x1bOB", b"j", b"\t"):
                selected = (selected + 1) % len(items)
            elif key in (b"\r", b"\n"):
                action, name = items[selected]
                if action == "new":
                    name = prompt_name(free_name())
                    if name is None:
                        continue
                return action, name
            elif key == b"n":
                name = prompt_name(free_name())
                if name is not None:
                    return "new", name
            elif key == b"x" and items[selected][0] == "attach":
                name = items[selected][1]
                os.write(1, f"\r\x1b[2KEnd session {name}? Its programs will be closed. [y/N] ".encode())
                if read_key() in (b"y", b"Y"):
                    ask({"op": "kill", "name": name})
                    time.sleep(0.2)
            elif key in (b"q", b"\x03", b"\x04", b"\x1b"):
                return "quit", None
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, saved)
        os.write(1, b"\x1b[H\x1b[2J")


def exec_shell() -> None:
    shell = os.environ.get("SHELL") or "/bin/sh"
    os.execv(shell, ["-" + os.path.basename(shell)])


def run_picker(login: bool, first: str | None = None, fork: str | None = None) -> int:
    """Attach to FIRST if it exists, then keep offering the picker."""
    parent = next((item for item in sessions() if item["name"] == fork), None) if fork else None
    if fork:
        # A fork is a new session in the directory the parent session is using.
        if attach(free_name(), create=True, cwd=(parent or {}).get("cwd")) == "exited" and not sessions():
            return 0
    elif first and any(item["name"] == first for item in sessions()):
        attach(first)
    elif login and not sessions():
        if attach(free_name(), create=True) == "exited" and not sessions():
            return 0
    while True:
        action, name = pick(login)
        if action == "shell":
            exec_shell()
        if action == "quit":
            return 0
        reason = attach(name, create=action == "new")
        if reason == "exited" and login and not sessions():
            return 0


def print_sessions() -> int:
    listing = sessions()
    if not listing:
        print("No qtty sessions. Start one with: qtty new [NAME]")
    for item in listing:
        state = "open elsewhere" if item["attached"] else "detached"
        print(f"{item['name']:<16} {item['command'] or '?':<12} active {ago(item['active'])} ago  {state}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="qtty", description=__doc__.splitlines()[0])
    parser.add_argument("--sessions", action="store_true", help="list sessions (same as qtty ls)")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("ls", help="list sessions")
    new = commands.add_parser("new", help="start a session and attach to it")
    new.add_argument("name", nargs="?")
    attach_cmd = commands.add_parser("attach", aliases=["a"], help="attach to a session")
    attach_cmd.add_argument("name")
    kill = commands.add_parser("kill", help="end a session and its programs")
    kill.add_argument("name")
    login = commands.add_parser("login", help="browser terminal entry point")
    login.add_argument("name", nargs="?")
    login.add_argument("--fork", metavar="NAME", help="start a new session in NAME's directory")
    commands.add_parser("serve", help=argparse.SUPPRESS)
    args = parser.parse_args()
    for value in (getattr(args, "name", None), getattr(args, "fork", None)):
        if value is not None and not NAME_RE.fullmatch(value):
            parser.error("session names use letters, digits, '.', '_' and '-' (up to 32)")
    if args.sessions or args.command == "ls":
        return print_sessions()
    if args.command == "serve":
        asyncio.run(serve())
        return 0
    if args.command == "kill":
        known = any(item["name"] == args.name for item in sessions())
        if not known:
            print(f"qtty: no session named {args.name}", file=sys.stderr)
            return 1
        return 0 if ask({"op": "kill", "name": args.name}).get("ok") else 1
    if args.command == "new":
        return 0 if attach(args.name or free_name(), create=True) != "missing" else 1
    if args.command in ("attach", "a"):
        return 0 if attach(args.name) != "missing" else 1
    if not os.isatty(0):
        return print_sessions()
    return run_picker(login=args.command == "login", first=getattr(args, "name", None),
                      fork=getattr(args, "fork", None))


if __name__ == "__main__":
    raise SystemExit(main())
