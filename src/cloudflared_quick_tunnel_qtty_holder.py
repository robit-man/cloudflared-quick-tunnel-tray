"""qtty session holder: keeps each terminal session's PTY open between attaches.

One holder runs per user. Clients connect over a Unix socket in a private
directory. On attach, the holder replays recent output and restores terminal
modes, then nudges the program to redraw.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import socket
import stat
import struct
import termios
import time
from pathlib import Path


NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
OUTPUT_BUFFER = 1024 * 1024
MAX_FRAME = 1024 * 1024
DETACH_KEY = b"\x1d"  # Ctrl-]
# Tells the browser page which session a pane shows (empty when detached).
OSC_SESSION = "\x1b]5379;qtty;{}\x07"
MODE_RE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
ALT_MODES = {47, 1047, 1049}
TRACKED_MODES = {1, 25, 1000, 1002, 1003, 1004, 1005, 1006, 1015, 2004} | ALT_MODES


def runtime_dir() -> Path:
    # QTTY_DIR lets tests run an isolated holder.
    path = Path(os.environ.get("QTTY_DIR") or f"/tmp/qtty-{os.getuid()}")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit(f"qtty: refusing to use {path}: it must be a private directory you own")
    return path


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("!I", len(payload)) + payload


class Frames:
    """Incremental parser for type byte + length + payload frames."""

    def __init__(self) -> None:
        self.data = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[bytes, bytes]]:
        self.data += chunk
        frames = []
        while len(self.data) >= 5:
            size = struct.unpack_from("!I", self.data, 1)[0]
            if size > MAX_FRAME:
                raise ValueError("qtty frame too large")
            if len(self.data) < 5 + size:
                break
            frames.append((bytes(self.data[:1]), bytes(self.data[5:5 + size])))
            del self.data[:5 + size]
        return frames


class Held:
    def __init__(self, name: str, pid: int, fd: int) -> None:
        self.name = name
        self.pid = pid
        self.fd = fd
        self.created = time.time()
        self.active = self.created
        self.buffer = bytearray()
        self.modes: dict[int, bool] = {}
        self.client: asyncio.StreamWriter | None = None

    def status(self) -> dict:
        command = cwd = None
        try:
            group = os.tcgetpgrp(self.fd)
            command = Path(f"/proc/{group}/comm").read_text().strip()
            cwd = os.readlink(f"/proc/{group}/cwd")
        except (OSError, ValueError):
            try:
                cwd = os.readlink(f"/proc/{self.pid}/cwd")
            except OSError:
                pass
        return {"name": self.name, "created": self.created, "active": self.active,
                "attached": self.client is not None, "command": command, "cwd": cwd}

    def record(self, data: bytes) -> None:
        self.active = time.time()
        for params, action in MODE_RE.findall(data):
            for value in params.split(b";"):
                if value.isdigit() and int(value) in TRACKED_MODES:
                    self.modes[int(value)] = action == b"h"
        self.buffer += data
        excess = len(self.buffer) - OUTPUT_BUFFER
        if excess > 0:
            # Start the replay on a line boundary to avoid a torn escape sequence.
            newline = self.buffer.find(b"\n", excess, excess + 4096)
            del self.buffer[:newline + 1 if newline >= 0 else excess]

    def replay(self) -> bytes:
        out = bytearray(b"\x1bc")  # Reset the client so the replay starts clean.
        in_alt = any(self.modes.get(mode) for mode in ALT_MODES)
        if in_alt and not re.search(rb"\x1b\[\?(?:47|1047|1049)h", self.buffer):
            out += b"\x1b[?1049h"
        out += self.buffer
        for mode, enabled in sorted(self.modes.items()):
            if mode not in ALT_MODES:
                out += b"\x1b[?%d%s" % (mode, b"h" if enabled else b"l")
        return bytes(out)


def set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def spawn(name: str, cols: int, rows: int, env: dict, cwd: str | None) -> Held:
    shell = env.get("SHELL") or os.environ.get("SHELL") or "/bin/sh"
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd or env.get("HOME") or "/")
        except OSError:
            os.chdir("/")
        env = {**env, "QTTY_SESSION": name}
        env.setdefault("TERM", "xterm-256color")
        for sig in (signal.SIGHUP, signal.SIGPIPE, signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_DFL)
        try:
            os.execve(shell, ["-" + os.path.basename(shell)], env)
        finally:
            os._exit(127)
    os.set_blocking(fd, False)
    set_size(fd, cols, rows)
    return Held(name, pid, fd)


class Holder:
    def __init__(self) -> None:
        self.sessions: dict[str, Held] = {}
        self.idle = asyncio.Event()

    def watch(self, held: Held) -> None:
        loop = asyncio.get_running_loop()

        def readable() -> None:
            try:
                data = os.read(held.fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if data:
                held.record(data)
                client = held.client
                if client is not None and not client.is_closing():
                    client.write(frame(b"d", data))
                    if client.transport.get_write_buffer_size() > OUTPUT_BUFFER:
                        # Pause the PTY until a slow client catches up.
                        loop.remove_reader(held.fd)
                        loop.call_later(0.05, resume)
                return
            loop.remove_reader(held.fd)
            self.finish(held)

        def resume() -> None:
            client = held.client
            if held.name not in self.sessions:
                return
            if client is not None and not client.is_closing() and \
                    client.transport.get_write_buffer_size() > OUTPUT_BUFFER // 4:
                loop.call_later(0.05, resume)
            else:
                loop.add_reader(held.fd, readable)

        loop.add_reader(held.fd, readable)

    def finish(self, held: Held) -> None:
        os.close(held.fd)
        try:
            _, status = os.waitpid(held.pid, 0)
            code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            code = None
        self.sessions.pop(held.name, None)
        if held.client is not None:
            held.client.write(frame(b"x", json.dumps({"code": code}).encode()))
            held.client.close()
        if not self.sessions:
            self.idle.set()

    async def write_input(self, held: Held, data: bytes) -> None:
        view = memoryview(data)
        while view and held.name in self.sessions:
            try:
                view = view[os.write(held.fd, view):]
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except OSError:
                return

    def nudge(self, held: Held, cols: int, rows: int) -> None:
        """Resize and force SIGWINCH so full-screen programs redraw."""
        try:
            set_size(held.fd, cols, max(1, rows - 1))
            asyncio.get_running_loop().call_later(0.05, set_size, held.fd, cols, rows)
        except OSError:
            pass

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("socket")
        uid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if uid != os.getuid():
            writer.close()
            return
        parser = Frames()
        held = None
        try:
            while chunk := await reader.read(65536):
                for kind, payload in parser.feed(chunk):
                    if held is None:
                        held = await self.request(kind, payload, writer)
                        if held is None:
                            return
                    elif kind == b"d":
                        await self.write_input(held, payload)
                    elif kind == b"r" and len(payload) == 4:
                        cols, rows = struct.unpack("!HH", payload)
                        set_size(held.fd, max(1, cols), max(1, rows))
        except (ValueError, ConnectionError, OSError):
            pass
        finally:
            if held is not None and held.client is writer:
                held.client = None
            writer.close()

    async def request(self, kind: bytes, payload: bytes, writer: asyncio.StreamWriter) -> Held | None:
        def reply(**fields) -> None:
            writer.write(frame(b"a", json.dumps(fields).encode()))

        request = json.loads(payload) if kind == b"q" else {}
        op, name = request.get("op"), request.get("name")
        if op == "list":
            reply(sessions=sorted((held.status() for held in self.sessions.values()), key=lambda s: s["created"]))
            return None
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            reply(ok=False, error="Session names use letters, digits, '.', '_' and '-' (up to 32).")
            return None
        if op == "kill":
            held = self.sessions.get(name)
            if held:
                os.killpg(os.getpgid(held.pid), signal.SIGHUP)
            reply(ok=bool(held))
            return None
        if op != "attach":
            reply(ok=False, error="Unknown request")
            return None
        cols, rows = int(request.get("cols", 80)), int(request.get("rows", 24))
        held = self.sessions.get(name)
        if held is None:
            if not request.get("create"):
                reply(ok=False, error=f"No session named {name}")
                return None
            env = request.get("env") if isinstance(request.get("env"), dict) else {}
            held = self.sessions[name] = spawn(name, cols, rows, env, request.get("cwd"))
            self.idle.clear()
            self.watch(held)
        if held.client is not None:
            held.client.write(frame(b"k"))
            held.client.close()
        held.client = writer
        reply(ok=True, name=name)
        replay = held.replay()
        for start in range(0, len(replay), 65536):
            writer.write(frame(b"d", replay[start:start + 65536]))
        self.nudge(held, cols, rows)
        return held


async def serve() -> None:
    directory = runtime_dir()
    lock = os.open(directory / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # Another holder is already running.
    path = directory / "control"
    path.unlink(missing_ok=True)
    holder = Holder()
    server = await asyncio.start_unix_server(holder.handle, path=str(path))
    os.chmod(path, 0o600)
    async with server:
        # Exit once the last session ends and no new one arrives.
        while True:
            await holder.idle.wait()
            await asyncio.sleep(2)
            if holder.idle.is_set() and not holder.sessions:
                break
    path.unlink(missing_ok=True)
